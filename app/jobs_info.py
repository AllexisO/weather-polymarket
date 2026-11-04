"""
Список скриптов крона — для страницы /status и проверок weather_alerts.py
(2026-09-27, просьба Alex: страница «Здоровье системы»).

Чистые данные, без импортов: файл читает и дашборд (в его контейнере нет зависимостей коллектора).
key — имя в job_runs/job_log; script — файл; log — файл в data/logs/ (как в кроне);
max_age_min — сколько минут можно не обновляться, прежде чем считать, что скрипт опаздывает.
Меняешь крон — поправь здесь.
"""

JOBS = [
    # key, подпись, script, расписание, max_age_min, log
    ("weather_edge", "Цены и прогнозы", "weather_edge.py", "каждые 2 ч", 160, "weather_edge.log"),
    ("weather_ml_fast", "Быстрый снимок обучаемых моделей (08:00)", "weather_ml_fast.py", ":02 и :32", 45, "weather_ml_fast.log"),
    ("weather_poly_resolve", "Итоги маркетов", "weather_poly_resolve.py", "каждые 2 ч, :05", 160, "weather_poly_resolve.log"),
    ("weather_paper", "Ставки и расчёт кошельков", "weather_paper.py", "каждые 2 ч, :10", 160, "weather_paper.log"),
    ("weather_real_dry", "Настоящие заявки вхолостую (общий банк: собрать и подписать заявку Polymarket, не отправлять)", "weather_real_dry.py", "каждые 2 ч, :25", 160, "weather_real_dry.log"),
    ("weather_pool_refill", "Докупка общего банка по живому стакану (двойник pool_refill: та же доля не дороже первой покупки, 6 ч)", "weather_pool_refill.py", "каждые 30 мин, :12 и :42", 45, "weather_pool_refill.log"),
    ("weather_fast_dry", "Быстрые решения в 08:00 вхолостую — fav, ml5_cal, ml5, ml4e_cal (сколько даёт скорость)", "weather_fast_dry.py", "каждый час с :55, живёт час", 90, "weather_fast_dry.log"),
    ("weather_fresh_live", "Свежий прогон ECMWF к 08:00 по каждому городу — запись для проверки v7 с подвижным k (без ставок)", "weather_fresh_live.py", "каждый час, :47", 90, "weather_fresh_live.log"),
    ("weather_obs_live", "Кошелёк по живым замерам", "weather_obs_live.py", "каждые 2 мин", 10, "weather_obs_live.log"),
    ("weather_copy_live", "Слушатель сделок (повтор за секунды)", "weather_copy_live.py", "всегда (copier)", 5, None),
    ("weather_copy", "Повтор за сильными трейдерами (запасной опрос)", "weather_copy.py", "каждые 5 мин", 15, "weather_copy.log"),
    ("weather_ens", "Сбор ансамблей", "weather_ens.py", "каждые 2 ч, :20", 180, "weather_ens.log"),
    ("weather_station_obs", "Замеры станций (METAR, факт)", "weather_station_obs.py", "каждые 6 ч, :30", 400, "weather_station_obs.log"),
    ("weather_multimodel", "16 погодных моделей", "weather_multimodel.py", "каждые 6 ч, :40", 400, "weather_multimodel.log"),
    ("weather_ml_data", "Прогнозные условия", "weather_ml_data.py", "каждые 6 ч, :50", 400, "weather_ml_data.log"),
    ("weather_trades_history", "Сбор настоящих сделок", "weather_trades_history.py", "04:30", 26 * 60, "weather_trades_history.log"),
    ("weather_sharp_rank", "Рейтинг сильных трейдеров", "weather_sharp_rank.py", "04:45", 26 * 60, "weather_sharp_rank.log"),
    ("weather_price_history", "История цен", "weather_price_history.py", "04:50", 26 * 60, "weather_price_history.log"),
    ("weather_ml_train", "Ночное обучение модели", "weather_ml_live.py", "05:20", 26 * 60, "weather_ml_train.log"),
    ("weather_ml_skill", "«Насколько модель права»", "weather_ml_skill.py", "05:50", 26 * 60, "weather_ml_skill.log"),
    # 20.10 (Alex: «чтобы при таких случаях ты сразу имел данные»): свежая копия базы для исследований каждое утро
    ("weather_research", "Копия базы для исследований", "weather_research.py", "06:40", 26 * 60, "weather_research.log"),
    ("weather_audit", "Проверка кошельков, моделей и базы", "weather_audit.py", "каждые 2 ч, :15; в 03:45 — с целостностью базы", 160, "weather_audit.log"),
    ("weather_night_check", "Проверки утро / день / вечер", "weather_night_check.py", "07:00, 13:00, 23:30", 11 * 60, "weather_night_check.log"),
    ("weather_pws_live", "Народные станции США (CWOP, сбор)", "weather_pws_live.py", "каждый час 11:00-19:00", 26 * 60, "weather_pws_live.log"),
    ("weather_alerts", "Проверки и тревоги", "weather_alerts.py", "каждые 30 мин", 70, "weather_alerts.log"),
    ("weather_logic_check", "Проверка логики — правила против итогов Polymarket (пн и чт)", "weather_logic_check.py", "пн и чт 09:00", 6000, "weather_logic_check.log"),
    ("weather_fastobs", "Быстрые замеры (Synoptic, JMA, DWD, FMI) + кошелёк obs_fast", "weather_fastobs.py", "каждую минуту", 5, "weather_fastobs.log"),
    ("weather_ml_day", "Дневная модель — решения в 10/12/14 местного (кошелёк ml_day)", "weather_ml_day.py", "каждый час в :06", 75, "weather_ml_day.log"),
    ("weather_ml_day_train", "Дневная модель — обучение и экзамен", "weather_ml_day.py --train", "05:45", 26 * 60, "weather_ml_day_train.log"),
    ("weather_llm_hour", "LLM-прогноз каждый час (llm_gem через OpenRouter и смеси llm_mix, llm_cal; llm_agy, llm_agy_nomem, llm_agy_p и двойник llm_gem_agy через agy, смеси llm_agy_cal и llm_mix5; llm_ds и llm_agy_bet выключены 01.11)", "weather_llm_hour.py", "каждый час в :56 (первый час дня — в :05)", 75, "weather_llm_hour.log"),
    ("weather_llm_skill", "Скилл agy с Python — кошелёк llm_skill (08:10 местного, 5 городов США)", "weather_llm_skill.py", "каждый час в :10", 75, "weather_llm_skill.log"),
    # 02.11: запись для проверки скорости на часовых BTC (без ставок, отдельная база data/research/crypto/live.sqlite3), 3 дня — разбор заметкой
    ("crypto_speed_rec", "Запись скорости BTC — поток Binance и стакан Polymarket с мс (часовые «вверх/вниз», без ставок)", "crypto_speed_rec.py", "каждый час в :59, живёт час", 75, "crypto_speed_rec.log"),
    # 03.11: стаканы всех погодных вариантов каждую минуту (без ставок, отдельные файлы data/booklog/books_ГГГГ-ММ.sqlite3)
    ("weather_book_rec", "Стаканы всех вариантов 47 городов каждую минуту — 30 уровней, только изменившиеся (без ставок, data/booklog)", "weather_book_rec.py", "каждый час с :55, живёт час", 25, "weather_book_rec.log"),
    # weather_obs_wethr — выключен 31.10 (заметка #59): подписка wethr.net отменена 15.10 (порог PRD §9 п.8 не прошёл), ключ до 01.11
    ("weather_obs_atis", "Живые замеры — ATIS (кошелёк obs_atis, atis.info, 11 городов США; до проверки — вхолостую)", "weather_obs_atis.py", "каждый час в :48, 15 мин", 75, "weather_obs_atis.log"),
    # weather_mm_ml, weather_mm_ws — выключены 27.10 (решение Alex): бот-мейкер целиком на «Не прошли»; weather_mm_settle снят 30.10 (заметка #122):
    # исполнения погодных маркетов рассчитаны все; скрипт остаётся — его L_MAX/REBATE берёт weather_logic_check
    # weather_obs_rt — выключен 15.10 (решение Alex): порог PRD §9 п.7 не прошёл; модуль остаётся — его функции берёт weather_obs_wethr
    # 02.10: weather_mm_paper (старый бот-мейкер, опрос раз в 30 с) отключён решением Alex — в кроне закомментирован
]

# имя файла скрипта -> key (у ночного обучения они разные)
KEY_BY_SCRIPT = {script: key for key, _l, script, _s, _a, _log in JOBS}

# 03.11 (Alex: «много скриптов, которые я не вижу»): страница «Система → Скрипты» (/system/jobs) — группы и служебные
# скрипты крона без отметки запуска (их состояние — по времени последней записи в лог). Новый скрипт в кроне без строки
# здесь или в JOBS — на странице в группе «Нет в списке» (сверка с data/night/crontab.txt).
GROUPS = [
    ("Ставки и кошельки", ["weather_paper", "weather_ml_fast", "weather_fast_dry", "weather_real_dry", "weather_pool_refill",
                           "weather_ml_day", "weather_llm_hour", "weather_llm_skill", "weather_obs_live", "weather_obs_atis",
                           "weather_fastobs", "weather_copy_live", "weather_copy"]),
    ("Данные: цены, прогнозы, замеры", ["weather_edge", "weather_poly_resolve", "weather_station_obs", "weather_multimodel",
                                        "weather_ml_data", "weather_ens", "weather_pws_live", "weather_trades_history",
                                        "weather_price_history", "weather_sharp_rank"]),
    ("Модели: обучение и оценка", ["weather_ml_train", "weather_ml_day_train", "weather_ml_skill"]),
    ("Запись без ставок (для проверок)", ["weather_book_rec", "weather_fresh_live", "crypto_speed_rec"]),
    ("Проверки и служебные", ["weather_alerts", "weather_audit", "weather_night_check", "weather_logic_check", "weather_research",
                              "db_watchdog", "backup_db", "agy_runner"]),
]

# key, подпись, скрипт, расписание, max_age_min, log — как JOBS, но без job_runs: «жив» = лог обновлялся не позже max_age_min
SERVICE_SCRIPTS = [
    ("db_watchdog", "Сторож базы — останавливает зависшие запуски, если база занята", "db_watchdog.sh", "каждые 2 мин", None, "db_watchdog.log"),   # пишет в лог, только когда вмешивается
    ("backup_db", "Ночная копия всех баз на другой диск (personal_data)", "backup_db.sh", "03:10", 26 * 60, "backup_db.log"),
    ("agy_runner", "Мост к Antigravity — отправляет письма LLM-кошельков llm_agy* и забирает ответы", "agy_runner.py", "каждую минуту", None, "agy_runner.log"),   # пишет в лог, только когда есть письма
]

# шапки служебных скриптов — сайт видит только app/ и data/, а эти файлы лежат в корне проекта (при правке шапки — поправь здесь)
SERVICE_DOCS = {
    "db_watchdog": """Сторож базы (2026-09-27, требование Alex: «если зависло — сразу откинуть»).
Крон каждые 2 минуты. Проверяет, что в базу можно писать (app/db_check.py, ждёт до 20 с).
3 неудачи подряд (~6 мин) — снимает виновника:
  1) останавливает скрипты крона (collector), идущие дольше 10 минут, — по одному, с самого долгого, после каждого проверяя,
     освободилась ли база;
  2) если не помогло — перезапускает слушатель (copier) и сайт (dashboard);
  3) пишет data/ALERT_DB_LOCKED — сайт показывает красную плашку 24 часа.
Лог: data/logs/db_watchdog.log (пишется только при проблемах).""",
    "backup_db": """Резервная копия всех баз weather-lab (2026-10-02): база на одном SSD без зеркала — копия на другом физическом диске
(пул personal_data, HDD). Крон каждую ночь в 03:10.
Как: VACUUM INTO из базы, открытой только на чтение, — целостная копия на один момент; проверка копии (PRAGMA quick_check);
сжатие zstd (~в 4 раза). Хранение: последние 7 дней + копии воскресений за 4 недели. Итог — data/backup_status.json.
Плюс архивы data/research (без копии базы для исследований), data/ml и ключи .env (права 600).
Не входит — список на странице «Бэкап» (в том числе data/booklog — стаканы каждую минуту).
Подробности и история копий — страница «Бэкап».""",
    "agy_runner": """Обработчик писем LLM-кошельков llm_agy* (09.10): Gemini через Antigravity CLI (agy).
agy стоит на сервере (~/.local/bin/agy, вход по аккаунту Google), а не в контейнере. Поэтому:
  weather_llm_hour.py (контейнер) кладёт письмо в data/agy/queue/<город>_<дата>_<час>.txt →
  этот скрипт (крон сервера раз в минуту) отдаёт его agy и пишет ответ в data/agy/done/<то же>.json →
  weather_llm_hour.py забирает ответ, считает шансы и ставит.
Исчерпан лимит Google — пауза на час (data/agy/paused_until): письма сразу получают ответ PAUSED.
Вторая очередь data/agy/skill_queue — письма кошелька llm_skill (agy с Python в изолированном контейнере, 3-5 мин), только когда
основная очередь пуста. Лог — data/logs/agy_runner.log, пишется только когда есть письма.""",
}

# файл в кроне → key, когда крон зовёт обёртку, а не сам скрипт
CRON_ALIAS = {"night_check.sh": "weather_night_check"}

# 04.11 (Alex «оживить, чтобы было видно, что и как записываем»): схема «Кто что записывает» на /system/flows —
# откуда скрипт берёт данные и куда пишет. Сверено с кодом 04.11 (INSERT/REPLACE INTO и адреса API в каждом скрипте).
# Меняешь, куда пишет скрипт, — поправь здесь. Таблица без префикса — рабочая база; «файл:…» — отдельный файл (FLOW_FILES).
FLOW_SOURCES = {
    "gamma": "Polymarket · маркеты", "clob": "Polymarket · стаканы", "data": "Polymarket · сделки",
    "ws": "Polymarket · поток сделок", "om": "Open-Meteo", "iem": "Iowa Mesonet (METAR)",
    "awc": "aviationweather / NOAA", "fast": "JMA · DWD · FMI · KNMI · HKO", "cwop": "CWOP (народные станции)",
    "atis": "ATIS", "openrouter": "OpenRouter (LLM)", "agy": "Antigravity (Gemini)", "binance": "Binance", "db": "рабочая база",
}
FLOW_FILES = {   # «файл:имя» -> (подпись, путь от /data)
    "файл:fast_dry": ("fast_dry.sqlite3", "db/fast_dry.sqlite3"),
    "файл:fastobs": ("fastobs.sqlite3", "db/fastobs.sqlite3"),
    "файл:booklog": ("booklog (месяц)", "booklog"),
    "файл:pws": ("pws.sqlite3", "db/pws.sqlite3"),
    "файл:research": ("research.sqlite3", "research/research.sqlite3"),
    "файл:crypto": ("crypto/live.sqlite3", "research/crypto/live.sqlite3"),
    "файл:backup": ("копии на personal_data", None),
    "файл:agy": ("data/agy (письма)", "agy"),
    "файл:ml": ("data/ml (модели)", "ml"),
}
FLOWS = {   # key -> (источники, куда пишет)
    "weather_edge": (["gamma", "om", "awc"], ["snapshots"]),
    "weather_multimodel": (["om"], ["mm_forecasts", "snapshots"]),
    "weather_ml_fast": (["gamma", "awc", "db"], ["snapshots_fast"]),
    "weather_poly_resolve": (["gamma"], ["weather_poly_outcomes"]),
    "weather_paper": (["clob", "db"], ["paper_trades", "decision_books"]),
    "weather_real_dry": (["clob", "db"], ["real_dry"]),
    "weather_pool_refill": (["clob", "db"], ["pool_refill"]),
    "weather_fast_dry": (["gamma", "clob", "awc"], ["файл:fast_dry"]),
    "weather_fresh_live": (["om"], ["fresh_hres_live"]),
    "weather_obs_live": (["awc", "fast", "clob"], ["paper_obs_trades", "metar_seen", "weather_poly_outcomes", "decision_books"]),
    "weather_obs_atis": (["atis", "awc", "clob"], ["metar_seen_src"]),
    "weather_fastobs": (["fast", "awc"], ["файл:fastobs", "paper_obs_trades"]),
    "weather_copy_live": (["ws"], ["paper_trades"]),
    "weather_copy": (["data", "clob"], ["paper_trades", "decision_books"]),
    "weather_ml_day": (["clob", "awc", "db"], ["ml_day_preds", "paper_trades", "decision_books"]),
    "weather_llm_hour": (["openrouter", "agy", "om", "awc", "clob"], ["llm_hour_preds", "paper_trades", "decision_books"]),
    "weather_llm_skill": (["agy", "clob", "db"], ["llm_hour_preds", "paper_trades"]),
    "weather_ens": (["om"], ["ens_forecasts"]),
    "weather_station_obs": (["iem"], ["station_obs", "weather_station_daily"]),
    "weather_ml_data": (["om", "iem"], ["ml_fcst_vars"]),
    "weather_pws_live": (["cwop"], ["файл:pws"]),
    "weather_trades_history": (["data"], ["poly_trades", "poly_trades_days", "poly_trade_wallets", "poly_market_final"]),
    "weather_price_history": (["clob"], ["price_history", "price_history_days", "weather_poly_outcomes"]),
    "weather_sharp_rank": (["gamma", "db"], ["sharp_wallets"]),
    "weather_ml_train": (["db"], ["ml_train_log", "файл:ml"]),
    "weather_ml_day_train": (["db"], ["ml_day_exam", "файл:ml"]),
    "weather_ml_skill": (["db"], ["ml_skill"]),
    "weather_book_rec": (["gamma", "clob"], ["файл:booklog"]),
    "crypto_speed_rec": (["binance", "gamma", "clob"], ["файл:crypto"]),
    "weather_alerts": (["db"], ["alerts"]),
    "weather_audit": (["db"], ["audit_log"]),
    "weather_night_check": (["db"], ["night_check"]),
    "weather_logic_check": (["db", "gamma"], ["logic_check", "logic_rules"]),
    "weather_research": (["db"], ["файл:research"]),
    "backup_db": (["db"], ["файл:backup"]),
    "agy_runner": (["agy"], ["файл:agy"]),
}
