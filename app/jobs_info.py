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
    ("weather_audit", "Проверка кошельков, моделей и базы", "weather_audit.py", "каждые 2 ч, :15; в 03:45 — с целостностью базы", 160, "weather_audit.log"),
    ("weather_night_check", "Проверки утро / день / вечер", "weather_night_check.py", "07:00, 13:00, 23:30", 11 * 60, "weather_night_check.log"),
    ("weather_pws_live", "Народные станции США (CWOP, сбор)", "weather_pws_live.py", "каждый час 11:00-19:00", 26 * 60, "weather_pws_live.log"),
    ("weather_alerts", "Проверки и тревоги", "weather_alerts.py", "каждые 30 мин", 70, "weather_alerts.log"),
    ("weather_fastobs", "Быстрые замеры (Synoptic, JMA, DWD, FMI) + кошелёк obs_fast", "weather_fastobs.py", "каждую минуту", 5, "weather_fastobs.log"),
    ("weather_llm_hour", "LLM-прогноз каждый час (кошельки llm_gem и llm_ds, OpenRouter, 5 городов США)", "weather_llm_hour.py", "каждый час в :05", 75, "weather_llm_hour.log"),
    ("weather_obs_wethr", "Живые замеры — wethr (кошелёк obs_wethr, платный поток wethr.net, 5 городов США)", "weather_obs_wethr.py", "каждый час с :07, 57 мин", 75, "weather_obs_wethr.log"),
    ("weather_obs_rt", "Живые замеры — быстро (кошелёк obs_rt, постоянный опрос NOAA)", "weather_obs_rt.py", "каждый час с :07, 57 мин", 75, "weather_obs_rt.log"),
    ("weather_netatmo", "Частные станции Netatmo у аэропортов (сбор для проверки)", "weather_netatmo.py", "каждые 5 мин", 20, "weather_netatmo.log"),
    # 02.10: weather_mm_paper (старый бот-мейкер, опрос раз в 30 с) отключён решением Alex — в кроне закомментирован
    ("weather_mm_ws", "Бот-мейкер на живом потоке", "weather_mm_ws.py", "каждый час, 57 мин", 75, "weather_mm_ws.log"),
    ("weather_mm_settle", "Бот-мейкер: расчёт исполнений", "weather_mm_settle.py", "каждые 2 ч, :25", 160, "weather_mm_settle.log"),
]

# имя файла скрипта -> key (у ночного обучения они разные)
KEY_BY_SCRIPT = {script: key for key, _l, script, _s, _a, _log in JOBS}
