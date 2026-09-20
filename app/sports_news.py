"""
Третья, ещё не проверенная гипотеза поверх sports_edge.py: новости о
составе команды могут двигаться быстрее, чем sharp-линия Pinnacle
успевает их учесть — а значит это независимый от Pinnacle И от цены
Polymarket сигнал (в отличие от погоды, тут источник — текст, не число,
поэтому нужен LLM, чтобы вытащить из заголовков структурированный сигнал).

ВАЖНО: это разведка, не готовая находка. Pinnacle — модель с высокой
скоростью реакции на такие новости (алгоритмическая книга), так что
велик шанс, что окно "новость есть, а линия ещё не подвинулась" либо
очень короткое, либо вообще не существует для топ-лиг. Прежде чем это
превратится в торговую идею — нужно накопить снимки и посмотреть,
действительно ли сигнал отсюда предсказывает будущее движение
pinnacle_p/market_p или итоговый исход лучше, чем сами по себе
sports_snapshots. Пока — только сбор и логирование, ничего не торгует.

Охват новостей ИЗНАЧАЛЬНО был нарочно широким (не только травмы): в начале
сезона травм было мало, а трансферное окно ещё открыто, значит именно
трансферы и смена тренера тогда были вероятнее источником хоть какого-то
сигнала для проверки, работает ли идея вообще.

2026-09-18: на /calibration набралось достаточно случаев (109), чтобы
увидеть — сигнал ровно совпадает с тем, во что и так верит рынок (45%/45%,
37%/35%), никакого опережения ни по сути, ни по времени. Раньше это было бы
рано интерпретировать (n мал), сейчас нет. Трансферное окно закрылось,
сезон идёт полным ходом — травмы и ротации состава теперь основной
источник новостей, а трансферный шум (слухи о переходах, которые
изначально помогали набрать объём) отвлекает LLM от реального сигнала.
Сузили запрос и типы сигнала до отсутствия/возвращения игрока — трансферы
и смена тренера больше не ищутся. Старые записи в sports_news_signal с
transfer_in/transfer_out/manager_change не удалены (история снимков не
переписывается, как и везде в проекте) — просто новых таких сигналов не
будет.

Источник новостей — Google News RSS по запросу о травмах/составе (см.
2026-09-18 выше), бесплатно, без ключа.
Извлечение сигнала — локальная LLM (Ollama, qwen3:8b) на сервере в LAN,
чтобы не простаивала и не тратить деньги на API. Модель ТОЛЬКО вытаскивает
структурированный сигнал из уже найденных заголовков и явно проинструктирована
игнорировать слухи/"линкуют с" — считать только подтверждённые изменения.
Не предсказывает исход сама и не имеет доступа к информации, которой нет
в переданных ей заголовках.
"""

import json
import os
import re
import sqlite3
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.1.16:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

NEWS_RSS = "https://news.google.com/rss/search"
NEWS_LOOKBACK_HOURS = 72
MAX_HEADLINES = 15

SYSTEM_PROMPT = """Ты вытаскиваешь структурированный сигнал из заголовков спортивных новостей
перед КОНКРЕТНЫМ футбольным матчем. Тебе дают название команды, название её СОПЕРНИКА в этом
матче и список заголовков за последние дни (заголовки могли найтись по имени команды и
могут быть про ЛЮБОЙ её матч или вообще не про матч).

Твоя задача — найти среди заголовков ПОДТВЕРЖДЁННЫЕ изменения СОСТАВА, которые могут повлиять
на силу команды именно в матче ПРОТИВ УКАЗАННОГО СОПЕРНИКА:
- отсутствие игрока (травма, дисквалификация, ротация/отдых)
- возвращение игрока после травмы или дисквалификации

ИГНОРИРУЙ трансферы, смену тренера, слухи, "связывают с", "могут подписать", "заинтересованы
в" — считай только подтверждённые отсутствия/возвращения игроков, официально или несколькими
источниками как факт. Если заголовок явно про игру с ДРУГИМ соперником и нет оснований думать,
что это всё ещё актуально к этому матчу — не считай сигналом (severity="none") или явно укажи
оговорку в note. НЕ придумывай ничего, чего нет в заголовках.

Ответь СТРОГО в формате JSON:
{"changes": [{"type": "absence|return",
"detail": "имя игрока и что случилось", "effect": "weakens|strengthens|unclear"}, ...],
"severity": "none|minor|major", "confidence": "low|medium|high",
"note": "одно предложение почему, включая явную оговорку, если заголовок был про другого
соперника или про слух, а не факт"}"""


def fetch_news(team):
    query = (
        f'"{team}" (injury OR suspended OR out OR lineup OR returns OR doubtful OR fitness OR benched)'
    )
    url = f"{NEWS_RSS}?{urllib.parse.urlencode({'q': query, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    items = []
    for item in root.iter("item"):
        title = item.findtext("title", "")
        pub_date = item.findtext("pubDate", "")
        try:
            dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt < cutoff:
            continue
        items.append(title)
        if len(items) >= MAX_HEADLINES:
            break
    return items


def extract_signal(team, opponent, headlines):
    if not headlines:
        return {"changes": [], "severity": "none", "confidence": "low", "note": "нет свежих заголовков"}

    user_prompt = f"Команда: {team}\nСоперник в этом матче: {opponent}\nЗаголовки:\n" + "\n".join(f"- {h}" for h in headlines)
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1},
        },
        timeout=90,
    )
    r.raise_for_status()
    content = r.json()["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"changes": [], "severity": "none", "confidence": "low", "note": f"не распарсился ответ модели: {content[:200]}"}

    parsed.setdefault("changes", [])
    parsed.setdefault("severity", "none")
    parsed.setdefault("confidence", "low")
    parsed.setdefault("note", "")
    return parsed


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sports_news_signal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            team TEXT,
            side TEXT,
            severity TEXT,
            confidence TEXT,
            changes TEXT,
            note TEXT,
            n_headlines INTEGER,
            headlines TEXT,
            model TEXT
        )
        """
    )
    conn.commit()


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)

    matches = conn.execute(
        """
        SELECT DISTINCT league, home_team, away_team, commence_time
        FROM sports_snapshots
        WHERE commence_time > ?
        """,
        (now.isoformat().replace("+00:00", "Z"),),
    ).fetchall()

    if not matches:
        print("Нет предстоящих сматченных матчей — нечего проверять на новости")
        conn.close()
        return

    rows = []
    for m in matches:
        for side, team, opponent in (("home", m["home_team"], m["away_team"]), ("away", m["away_team"], m["home_team"])):
            try:
                headlines = fetch_news(team)
                signal = extract_signal(team, opponent, headlines)
            except requests.RequestException as e:
                print(f"{team}: ошибка — {e}", file=sys.stderr)
                continue

            rows.append(
                (
                    now.isoformat(),
                    m["league"],
                    m["home_team"],
                    m["away_team"],
                    m["commence_time"],
                    team,
                    side,
                    signal["severity"],
                    signal["confidence"],
                    json.dumps(signal["changes"], ensure_ascii=False),
                    signal["note"],
                    len(headlines),
                    json.dumps(headlines, ensure_ascii=False),
                    OLLAMA_MODEL,
                )
            )
            if signal["severity"] != "none":
                print(f"{team} ({side}, {m['home_team']} vs {m['away_team']}): "
                      f"severity={signal['severity']} confidence={signal['confidence']} — {signal['note']}")

    if rows:
        conn.executemany(
            """
            INSERT INTO sports_news_signal
            (ts_utc, league, home_team, away_team, commence_time, team, side, severity, confidence, changes, note, n_headlines, headlines, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    n_signal = sum(1 for r in rows if r[7] != "none")
    print(f"Проверено команд: {len(rows)} ({len(matches)} матчей), из них с сигналом: {n_signal}")
    conn.close()


if __name__ == "__main__":
    run()
