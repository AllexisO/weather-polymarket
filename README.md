# weather-polymarket

Поиск честного преимущества (edge) на погодных маркетах [Polymarket](https://polymarket.com)
«Highest temperature in X» — 48 городов, итог по METAR.

> **Только виртуальные деньги.** Все ставки — paper trading на виртуальных кошельках, с реалистичной
> имитацией исполнения (живой стакан, задержка, комиссия). Это исследовательский проект, не торговый
> совет.

*English: research project looking for an edge in Polymarket daily-high-temperature markets.
LightGBM quantile model on 16 weather models + METAR + market price; results are tracked with
paper wallets against live order books. No real money.*

## Идея

Рынок погоды очень точен: цена варианта ≈ частота, с которой он выигрывает. Поэтому:

1. Модель предсказывает распределение максимальной температуры на день.
2. Ставка делается, только когда модель заметно спорит с рынком (перевес ≥ порога).
3. Каждая идея — отдельный виртуальный кошелёк; кошельки сравниваются по доходности в % от поставленного.
4. Преимущество проверяется по **настоящим сделкам** с комиссией, а не по истории цен.

## Как устроено

```
Polymarket, Open-Meteo (16 моделей), METAR
        │  скрипты по крону (docker compose run collector ...)
        ▼
SQLite: снимки цен и прогнозов, факт, итоги, сделки
        │  ночное переобучение модели
        ▼
Модель v3: LightGBM, квантильная регрессия (13 уровней), одна на все города
        │  признаки на 08:00 местного: прогнозы и их разброс, утренние METAR, сезон, мнение рынка
        ▼
Виртуальные кошельки ──► сайт (FastAPI): счёт, ставки сценариями, точность модели
```

Подробно — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

| Часть | Что |
|---|---|
| `app/weather_*.py` | сбор данных, модель, кошельки, исследования |
| `app/polyexec.py` | имитация исполнения: стакан, задержка 2 с, комиссия, минимум 5 долей, стоп-файл |
| `app/dashboard.py` + `app/templates/` | сайт на порту 8093 |
| `check.sh` | обязательная проверка после изменений: сборка, колонки, прогон кошельков в памяти, страницы |
| `db_watchdog.sh` | сторож базы: снимает зависшие скрипты |

Стек: Python 3.12, LightGBM, SQLite (WAL), FastAPI + Jinja2, Docker Compose.

## Запуск

```bash
git clone git@github.com:AllexisO/weather-polymarket.git && cd weather-polymarket
cp .env.example .env                         # ключи не нужны; файл нужен docker compose
docker compose build
docker compose up -d dashboard copier        # сайт http://localhost:8093 и слушатель сделок
docker compose run --rm collector weather_edge.py   # первый снимок цен и прогнозов
```

База создаётся в `data/db/polymarket_lab.sqlite3` (в git не входит). Для работы нужен крон —
расписание всех скриптов в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), например:

```cron
0 */2 * * *  cd /path/to/weather-polymarket && docker compose run --rm collector weather_edge.py
10 */2 * * * cd /path/to/weather-polymarket && docker compose run --rm collector weather_paper.py
20 5 * * *   cd /path/to/weather-polymarket && docker compose run --rm -e JOB_TIMEOUT=10800 collector weather_ml_live.py --train
```

Модели обучаются на накопленной истории (с 2025-06-01); на пустой базе сначала нужна загрузка истории
(`weather_history_extend.py` — прогнозы и METAR, `weather_price_history.py` — цены рынка).

## Документы

- [`docs/PRD.md`](docs/PRD.md) — цель, метрики, порог перехода к реальным деньгам, что уже проверено и не сработало
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — схема, крон, таблицы, модель, кошельки
- [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md) — дизайн сайта
- [`CLAUDE.md`](CLAUDE.md) — правила для ИИ-агента, работающего с проектом

## Источники данных

Polymarket (Gamma, CLOB, Data API), [Open-Meteo](https://open-meteo.com), METAR —
[Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu) и aviationweather.gov. Все бесплатные.
