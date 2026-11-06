"""06.11: Python загружает этот файл сам при старте (PYTHONPATH=/app в docker-compose, collector) — счётчик запросов к
Open-Meteo у ручных запусков и исследований (om_usage.py). Запуски крона считает job_wrap.py (OM_WRAPPED=1 — пропускаем)."""
import os

if not os.environ.get("OM_WRAPPED"):
    try:
        import om_usage
        om_usage.install()
    except Exception:
        pass
