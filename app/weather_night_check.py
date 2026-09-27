"""
Вечерняя проверка перед ночью — 23:30 (2026-09-28, просьба Alex: «видеть и починить до сна, чтобы ночью
ничего не сломалось и не застряло: обучение, ставки»).

Проверяет всё, от чего зависит ночь (сбор сделок 04:30, рейтинг 04:45, история цен 04:50, обучение 05:20,
«насколько права» 05:50, ставки каждые 2 ч):
  1. код: сборка скриптов и прогон всех кошельков в памяти (preflight);
  2. ставки: полная проверка (weather_audit) — нарушения правил, кошельки без денег;
  3. крон: каждый скрипт есть в расписании, ошибки и пропуски за сутки, опоздания, зависшие запуски;
  4. база: запись проходит, журнал WAL не разросся, место на диске;
  5. внешние сервисы: Polymarket, Open-Meteo, METAR, FMI отвечают;
  6. к обучению: файлы моделей открываются, есть вчерашний факт и свежие прогнозы, прошлое обучение прошло;
  7. лимит Open-Meteo за сутки, стоп ставок.

Каждая проверка — ok / warn (внимание) / bad (ночью сломается). Итог — в night_check (JSON), страница /audit
показывает его сверху; bad → weather_alerts поднимает красную плашку.
Запуск: night_check.sh (крон 23:30) — он снимает crontab и docker ps хоста в data/night/.
"""

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from jobmark import item_guard, mark
from jobs_info import JOBS

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
DATA = DB_PATH.parent.parent
ML = DATA / "ml"
OPEN_METEO_DAILY = 10000
CHECKS = []
# 2026-09-28 (решение Alex): три проверки в день — утро «ночь прошла?», день «утренние решения приняты?», вечер «ночь пройдёт?»
MODES = {"morning": "Утро", "midday": "День", "evening": "Вечер"}
TZ_HOME = "Europe/Chisinau"
NIGHT_JOBS = ["weather_trades_history", "weather_sharp_rank", "weather_price_history", "weather_ml_train", "weather_ml_skill"]


def add(group, ok, title, detail="", fix=""):
    """ok: True — в порядке, "warn" — внимание, False — ночью сломается."""
    level = "ok" if ok is True else ("warn" if ok == "warn" else "bad")
    CHECKS.append({"group": group, "level": level, "title": title, "detail": detail, "fix": fix})


def check_code():
    import preflight
    for ok, name, detail in preflight.run_checks():
        add("Код", ok, name, detail, "" if ok else "запустить ./check.sh и починить то, что он покажет")


def check_audit():
    import weather_audit
    weather_audit.run(deep=False)
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    r = c.execute("SELECT details FROM audit_log ORDER BY run_at DESC LIMIT 1").fetchone()
    c.close()
    a = json.loads(r[0])
    add("Ставки", not a["n_violations"], f"Ставки против правил и итогов: проверено {a['n_bets']}",
        f"нарушений {a['n_violations']}" + (": " + "; ".join(a["violations"][:3]) if a["violations"] else ""),
        "открыть страницу «Проверка» — там список по кошелькам")
    broke = [w for w in a["wallets"] if w["key"] not in ("obs", "obs_fmi") and w["cash"] < 2.0]
    add("Ставки", "warn" if broke else True, "У всех кошельков есть деньги на ставку",
        ", ".join(f"{w['key']} ${w['cash']:.2f}" for w in broke) or "у всех ≥ $2",
        "решить: пополнить или оставить — такие кошельки ночью ставить не будут" if broke else "")


def check_cron(c, now):
    tab = (DATA / "night" / "crontab.txt")
    lines = tab.read_text().splitlines() if tab.exists() else []
    active = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    if not lines:
        add("Крон", "warn", "Расписание крона", "не удалось прочитать (запуск не через night_check.sh)")
    else:
        # в кроне скрипт может стоять и через свой .sh (вечерняя проверка — night_check.sh)
        names = lambda script: (script, script.replace("weather_", "").replace(".py", ".sh"))
        miss = [label for key, label, script, *_ in JOBS if key != "weather_copy_live"
                and not any(n in l for l in active for n in names(script))]
        add("Крон", not miss, "Все скрипты стоят в расписании", ", ".join(miss) or f"{len(JOBS) - 1} скриптов на месте",
            "вернуть строку в crontab (docs/ARCHITECTURE.md — расписание)" if miss else "")
    # 2026-09-28 (просьба Alex): по-человечески и с учётом отметок «исправлено» (fixes.py)
    from fixes import failures
    open_, fixed = failures(c, now - timedelta(days=1), {k: label for k, label, *_ in JOBS})
    add("Крон", "warn" if open_ else True, "Ошибки скриптов за сутки",
        "; ".join(open_) or ("новых нет" + (" — все прошлые исправлены" if fixed else "")),
        "посмотреть «Здоровье системы» — там строка ошибки из лога; после исправления отметить: fixes.py <скрипт> \"что исправлено\"" if open_ else "")
    for f in fixed:
        add("Крон", True, "Исправлено", f)
    runs = {r[0]: r[1] for r in c.execute("SELECT job, finished_at FROM job_runs")}
    late = []
    for key, label, _s, _sch, age, _log in JOBS:
        t = runs.get(key)
        if t:
            d = datetime.fromisoformat(t)
            d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            if now - d > timedelta(minutes=age):
                late.append(f"{label} ({(now - d).total_seconds() / 3600:.0f} ч)")
    add("Крон", not late, "Никто не опаздывает", ", ".join(late) or "все отработали вовремя",
        "посмотреть лог скрипта в data/logs/ и запустить вручную" if late else "")
    ps = DATA / "night" / "docker_ps.txt"
    if ps.exists():
        rows = [l.split("|") for l in ps.read_text().splitlines() if l.count("|") >= 3]
        stuck = [r[0] for r in rows if "collector" in r[0] and r[1] == "running"
                 and any(u in r[2] for u in ("hour", "day"))]
        add("Крон", not stuck, "Нет зависших запусков", ", ".join(stuck) or "зависших нет",
            "остановить: sudo docker stop <имя> (сторож базы делает это сам, если база занята)" if stuck else "")
        for name in ("weather-lab-copier", "weather-lab-dashboard"):
            r = next((r for r in rows if r[0] == name), None)
            add("Крон", bool(r and r[1] == "running"), f"{'Слушатель сделок (copier)' if 'copier' in name else 'Сайт'} работает",
                r[3] if r else "контейнер не найден", f"sudo docker compose up -d {name.split('-')[-1]}")


def check_db(c):
    try:
        w = sqlite3.connect(DB_PATH, timeout=10)
        w.execute("BEGIN IMMEDIATE")
        w.rollback()
        w.close()
        add("База", True, "Запись в базу проходит", "блокировки нет")
    except sqlite3.Error as e:
        add("База", False, "Запись в базу проходит", f"{type(e).__name__}: {e}", "смотреть db_watchdog.log; сторож снимет зависший скрипт")
    add("База", c.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "Режим WAL", "")
    wal = Path(str(DB_PATH) + "-wal")
    size = wal.stat().st_size if wal.exists() else 0
    add("База", True if size < 500e6 else "warn", "Журнал записи (WAL) не разросся", f"{size / 1e6:.0f} МБ",
        "что-то долго держит чтение базы — проверить зависшие скрипты" if size >= 500e6 else "")
    du = shutil.disk_usage(DB_PATH.parent)
    pct = du.free / du.total * 100
    add("База", True if pct > 15 else ("warn" if pct > 5 else False), "Место на диске", f"свободно {du.free / 1e9:.0f} ГБ ({pct:.0f}%)",
        "освободить место — ночное обучение и сбор сделок пишут в базу" if pct <= 15 else "")


def check_services():
    probes = [
        ("Polymarket (маркеты)", "https://gamma-api.polymarket.com/events", {"limit": 1}),
        ("Polymarket (сделки)", "https://data-api.polymarket.com/trades", {"limit": 1}),
        ("Polymarket (стакан)", "https://clob.polymarket.com/time", None),
        ("Open-Meteo", "https://api.open-meteo.com/v1/forecast", {"latitude": 51.5, "longitude": 0, "daily": "temperature_2m_max", "forecast_days": 1}),
        ("METAR (Iowa Mesonet)", "https://mesonet.agron.iastate.edu/json/network.py", {"network": "FI__ASOS"}),
        ("METAR онлайн (aviationweather)", "https://aviationweather.gov/api/data/metar", {"ids": "EFHK", "format": "json"}),
        ("FMI (Хельсинки)", "https://opendata.fmi.fi/wfs", {"service": "WFS", "request": "GetCapabilities"}),
    ]
    for name, url, params in probes:
        with item_guard(name):
            t0 = time.time()
            try:
                r = requests.get(url, params=params, timeout=20)
                ok = r.status_code < 500
                add("Сервисы", ok if ok else False, name, f"ответ {r.status_code} за {time.time() - t0:.1f} с",
                    "" if ok else "сервис недоступен — ночью скрипты пропустят эти данные и догрузят позже")
            except requests.RequestException as e:
                add("Сервисы", False, name, f"не отвечает: {type(e).__name__}", "проверить интернет на сервере; если это их сбой — ждать")


def check_models(c, now):
    import lightgbm as lgb
    sets = [("v1", ML / "model.txt", None), ("v2", ML / "q", None), ("v3 — главная", ML / "q_mkt", ML / "q_mkt" / "features.json"),
            ("v4", ML / "q_mkt31", ML / "q_mkt31" / "features.json")] + \
           [(f"v4e (зерно {s})", ML / f"q_mkt31_s{s}", None) for s in (11, 22, 33)]
    for name, path, feats in sets:
        with item_guard(name):
            f = path if path.is_file() else (path / "q50.txt")
            if not f.exists():
                add("Обучение", False, f"Модель {name} на месте", f"нет файла {f.relative_to(DATA)}", "запустить обучение вручную: weather_ml_live.py --train")
                continue
            b = lgb.Booster(model_file=str(f))
            detail = f"{b.num_feature()} признаков, обновлена {datetime.fromtimestamp(f.stat().st_mtime).strftime('%d.%m %H:%M')}"
            ok = True
            if feats and feats.exists():
                n = len(json.loads(feats.read_text()))
                if n != b.num_feature():
                    ok, detail = False, f"в модели {b.num_feature()} признаков, в features.json {n}"
            age_h = (time.time() - f.stat().st_mtime) / 3600
            add("Обучение", ok if age_h < 30 else "warn", f"Модель {name} открывается", detail + ("" if age_h < 30 else f" — не обновлялась {age_h:.0f} ч"),
                "" if ok else "переобучить: weather_ml_live.py --train")
    y = (now - timedelta(days=1)).date().isoformat()
    n_fact = c.execute("SELECT COUNT(*) FROM weather_station_daily WHERE local_date = ?", (y,)).fetchone()[0]
    add("Обучение", True if n_fact >= 44 else "warn", "Есть вчерашний факт по городам", f"{n_fact} из 48 за {y[8:10]}.{y[5:7]}",
        "запустить weather_station_obs.py — без факта ночное обучение возьмёт меньше данных" if n_fact < 44 else "")
    last_mm = c.execute("SELECT MAX(fetched_at) FROM mm_forecasts").fetchone()[0] \
        if "fetched_at" in [r[1] for r in c.execute("PRAGMA table_info(mm_forecasts)")] else None
    if last_mm:
        d = datetime.fromisoformat(last_mm)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        h = (now - d).total_seconds() / 3600
        add("Обучение", h < 8, "Прогнозы 16 моделей свежие", f"последняя загрузка {h:.0f} ч назад", "запустить weather_multimodel.py" if h >= 8 else "")
    r = c.execute("SELECT trained_at, ok FROM ml_train_log WHERE details NOT LIKE '%\"dry_run\": true%' ORDER BY trained_at DESC LIMIT 1").fetchone()
    if r:
        add("Обучение", bool(r[1]), "Прошлое ночное обучение прошло проверки", f"от {r[0][8:10]}.{r[0][5:7]} {r[0][11:16]} UTC",
            "открыть страницу «Обучение модели» — какая проверка не прошла" if not r[1] else "")


def check_night_done(c, now):
    """Утро: ночные скрипты отработали сегодня (после 03:00 по Кишинёву), обучение прошло, целостность базы проверена."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TZ_HOME)
    since = datetime.now(tz).replace(hour=3, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    runs = {r[0]: r[1] for r in c.execute("SELECT job, finished_at FROM job_runs")}
    labels = {k: label for k, label, *_ in JOBS}
    for key in NIGHT_JOBS:
        t = runs.get(key)
        d = datetime.fromisoformat(t) if t else None
        d = d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)
        done = d is not None and d >= since
        add("Ночь", done, f"Ночью отработал: {labels.get(key, key)}",
            f"в {d.astimezone(tz):%H:%M}" if done else ("ещё не отрабатывал" if not d else f"последний раз {d.astimezone(tz):%d.%m %H:%M}"),
            "" if done else f"посмотреть data/logs/ и запустить вручную: docker compose run --rm collector {next((s for k, _l, s, *_ in JOBS if k == key), key)}")
    r = c.execute("SELECT trained_at, ok FROM ml_train_log WHERE details NOT LIKE '%\"dry_run\": true%' ORDER BY trained_at DESC LIMIT 1").fetchone()
    if r:
        t = datetime.fromisoformat(r[0])
        fresh = t >= since
        add("Ночь", (bool(r[1]) if fresh else False), "Модель переобучилась этой ночью и прошла проверки",
            f"обучение {t.astimezone(tz):%d.%m %H:%M}" + ("" if r[1] else " — не все проверки"),
            "" if fresh and r[1] else "открыть «Обучение модели»; при необходимости — weather_ml_live.py --train")
    if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'audit_log'").fetchone():
        r = c.execute("SELECT run_at, details FROM audit_log WHERE deep = 1 ORDER BY run_at DESC LIMIT 1").fetchone()
        if r:
            t = datetime.fromisoformat(r[0])
            qc = json.loads(r[1])["db"].get("quick_check")
            add("Ночь", (qc == "ok") if t >= since else "warn", "Ночная проверка целостности базы",
                f"{qc} · {t.astimezone(tz):%d.%m %H:%M}", "" if qc == "ok" else "база повреждена — остановить крон и восстановить из копии")


def check_decisions(c, now):
    """День: у городов, где 08:00 уже прошло (окно решения до 12:00), есть решение каждого кошелька на сегодня."""
    from zoneinfo import ZoneInfo
    import weather_paper as wp
    from weather_cities import OBS_CITIES
    due = {}
    for city, cfg in OBS_CITIES.items():
        loc = datetime.now(ZoneInfo(cfg["tz"]))
        if loc.hour >= 12:
            due[city] = loc.date().isoformat()
    if not due:
        add("Решения", True, "Утренние решения", "ни в одном городе ещё нет 12:00")
        return
    have = {}
    for w, city, d in c.execute("SELECT wallet, city, local_date FROM paper_trades WHERE local_date >= ?", (min(due.values()),)):
        have.setdefault(w, set()).add((city, d))
    wallets = sorted(set(wp.WALLETS) | set(wp.NO_WALLETS) | set(wp.MAKER_WALLETS))
    for w in ["ml3"] + [x for x in wallets if x != "ml3"]:
        miss = [city for city, d in due.items() if (city, d) not in have.get(w, set())]
        main = w == "ml3"
        if main or miss:
            add("Решения", (True if not miss else ("warn" if not main else False)),
                f"{'Главная модель' if main else w}: решение по каждому городу, где прошло 08:00",
                f"решено {len(due) - len(miss)} из {len(due)}" + (f"; нет: {', '.join(miss[:8])}" + (" …" if len(miss) > 8 else "") if miss else ""),
                "посмотреть лог weather_paper / weather_ml_fast — был ли снимок в 08:00 по этим городам" if miss else "")
    others_ok = [w for w in wallets if w != "ml3" and not [c2 for c2, d in due.items() if (c2, d) not in have.get(w, set())]]
    if others_ok:
        add("Решения", True, "Остальные кошельки приняли решения", f"{len(others_ok)} из {len(wallets) - 1} — по всем городам")


def check_misc(c, now):
    day = (now - timedelta(days=1)).isoformat()
    om = c.execute("SELECT COALESCE(SUM(om_calls), 0) FROM job_log WHERE finished_at >= ?", (day,)).fetchone()[0]
    add("Прочее", True if om < 0.8 * OPEN_METEO_DAILY else "warn", "Лимит Open-Meteo за сутки", f"{om} запросов из ~{OPEN_METEO_DAILY}",
        "близко к лимиту — ночные загрузки могут не пройти" if om >= 0.8 * OPEN_METEO_DAILY else "")
    stop = (DATA / "STOP").exists()
    add("Прочее", "warn" if stop else True, "Стоп ставок выключен", "файл data/STOP есть — ночью ставок не будет" if stop else "ставки идут",
        "удалить data/STOP, если стоп не нужен" if stop else "")


def main():
    import sys
    mode = next((a for a in sys.argv[1:] if a in MODES), "evening")
    t0 = time.time()
    now = datetime.now(timezone.utc)
    for fn in (check_code, check_audit):
        with item_guard(fn.__name__):
            fn()
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    extra = {"morning": [(check_night_done, (c, now))], "midday": [(check_decisions, (c, now))]}.get(mode, [])
    for fn, args in extra + [(check_cron, (c, now)), (check_db, (c,)), (check_models, (c, now)), (check_misc, (c, now))]:
        with item_guard(fn.__name__):
            fn(*args)
    c.close()
    check_services()
    import jobmark
    for e in jobmark.ITEM_ERRORS:  # сама проверка не смогла что-то проверить — это тоже проблема
        add("Проверка", False, "Часть проверки не выполнилась", e, "посмотреть data/logs/weather_night_check.log")
    bad = sum(x["level"] == "bad" for x in CHECKS)
    warn = sum(x["level"] == "warn" for x in CHECKS)
    res = {"run_at": now.isoformat(), "mode": mode, "mode_name": MODES[mode], "took_s": round(time.time() - t0, 1),
           "bad": bad, "warn": warn, "checks": CHECKS}
    w = sqlite3.connect(DB_PATH, timeout=60)
    try:
        w.execute("CREATE TABLE IF NOT EXISTS night_check (run_at TEXT PRIMARY KEY, bad INTEGER, warn INTEGER, details TEXT)")
        if "mode" not in [r[1] for r in w.execute("PRAGMA table_info(night_check)")]:
            w.execute("ALTER TABLE night_check ADD COLUMN mode TEXT DEFAULT 'evening'")
        w.execute("INSERT OR REPLACE INTO night_check (run_at, bad, warn, details, mode) VALUES (?, ?, ?, ?, ?)",
                  (now.isoformat(), bad, warn, json.dumps(res, ensure_ascii=False), mode))
        w.execute("DELETE FROM night_check WHERE run_at < ?", ((now - timedelta(days=60)).isoformat(),))
        mark(w, "weather_night_check")
        w.commit()
    except Exception:
        w.rollback()
        raise
    finally:
        w.close()
    print(f"проверка «{MODES[mode]}»: {len(CHECKS)} проверок, проблем {bad}, внимание {warn}, за {res['took_s']} с")
    for x in CHECKS:
        if x["level"] != "ok":
            print(f"  {'ПРОБЛЕМА' if x['level'] == 'bad' else 'внимание'}: [{x['group']}] {x['title']} — {x['detail']}")


if __name__ == "__main__":
    main()
