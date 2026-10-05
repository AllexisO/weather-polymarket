"""
Текстовые разборы прогноза метеорологов NOAA (Area Forecast Discussion,
AFD) -> признаки для обучаемой модели через локальную LLM (2026-09-25,
идея Alex: "давать LLM данные, чтобы прогноз становился точнее").

Метеорологи каждого регионального офиса NWS несколько раз в день пишут
текстом, что ожидают и почему: "морской бриз ограничит прогрев",
"облачность до обеда", "наш максимум выше, чем у моделей". В числах
моделей этого нет. Qwen3 8B (Ollama в LAN, OLLAMA_URL) читает последний
разбор, выпущенный до 07:50 местного (как и остальные признаки
weather_ml), и вытаскивает факты в JSON. LLM ничего не прогнозирует
сама — только извлекает сказанное в тексте.

Только города США (NWS) — 11 из 48. Архив разборов — Iowa Mesonet (AFOS),
им же пользуемся и для живых (появляются там через минуты).

Таблица afd_signals: одна строка на город и день.
Запуск: python weather_afd.py            — недостающие дни (крон, каждый час)
        python weather_afd.py --backfill — вся история с HISTORY_START
"""

import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
OLLAMA_URL = os.environ.get("OLLAMA_URL")  # адрес сервера в домашней сети — только в .env (05.10: не светить в github)
OLLAMA_MODEL = "qwen3:8b"
AFOS = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
HISTORY_START = date(2026, 6, 1)
DECISION = (7, 50)  # последний разбор до 07:50 местного — как и замеры для weather_ml

# город -> (офис NWS, как назвать станцию в запросе к LLM)
WFO = {
    "nyc": ("OKX", "LaGuardia Airport / New York City"),
    "atlanta": ("FFC", "Hartsfield-Jackson Airport / Atlanta"),
    "miami": ("MFL", "Miami International Airport / Miami"),
    "los_angeles": ("LOX", "Los Angeles International Airport (LAX, coastal)"),
    "chicago": ("LOT", "O'Hare Airport / Chicago"),
    "dallas": ("FWD", "Dallas Love Field / Dallas"),
    "san_francisco": ("MTR", "San Francisco International Airport (SFO)"),
    "houston": ("HGX", "Houston Hobby Airport / Houston"),
    "denver": ("BOU", "Buckley Space Force Base / Denver metro"),
    "seattle": ("SEW", "Seattle-Tacoma Airport / Seattle"),
    "austin": ("EWX", "Austin-Bergstrom Airport / Austin"),
}
SKIP_SECTIONS = ("AVIATION", "MARINE", "HYDROLOGY", "TIDES", "FIRE", "CLIMATE", "WATCHES", "WARNINGS", "EQUIPMENT")

PROMPT = """You read a US National Weather Service Area Forecast Discussion. It was issued on {issued} local time.
Focus ONLY on the daytime HIGH temperature on {day} at {station}.
Answer with JSON only:
{{"high_f": number or null,
"vs_guidance": "warmer" or "cooler" or "same" or "unknown",
"sea_breeze": true or false or null,
"clouds_limit_heating": true or false or null,
"rain_today": true or false or null,
"front_today": true or false or null,
"confidence": "low" or "medium" or "high" or "unknown"}}
high_f: the forecaster's stated high for this area on that day in °F, only if explicitly given (a range -> its middle).
vs_guidance: does the forecaster say the high will be above/below model guidance (NBM, MOS, models)?
sea_breeze: sea or lake breeze expected to affect temperatures there that day.
Use only the text. If something is not mentioned, use null or "unknown".

TEXT:
{text}"""


def ensure_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS afd_signals (
            city TEXT NOT NULL, local_date TEXT NOT NULL, wfo TEXT, issued_utc TEXT,
            high_f REAL, vs_guidance TEXT, sea_breeze INTEGER, clouds_limit INTEGER, rain_today INTEGER,
            front_today INTEGER, confidence TEXT, raw TEXT,
            PRIMARY KEY (city, local_date))"""
    )
    conn.commit()


def latest_afd(wfo, before_utc):
    r = requests.get(AFOS, params={"pil": f"AFD{wfo}", "sdate": (before_utc - timedelta(hours=18)).strftime("%Y-%m-%dT%H:%MZ"),
                                   "edate": before_utc.strftime("%Y-%m-%dT%H:%MZ"), "fmt": "text", "limit": 1}, timeout=60)
    r.raise_for_status()
    t = r.text.split("\x03")[0].replace("\x01", "").strip()
    m = re.search(r"FXUS\d\d K\w{3} (\d{2})(\d{2})(\d{2})", t)
    if not m or "Area Forecast Discussion" not in t:
        return None, None
    day, hh, mm_ = int(m.group(1)), int(m.group(2)), int(m.group(3))
    issued = before_utc.replace(day=day, hour=hh, minute=mm_, second=0, microsecond=0) if day <= before_utc.day else \
        (before_utc - timedelta(days=1)).replace(day=day, hour=hh, minute=mm_, second=0, microsecond=0)
    blocks = re.split(r"\n(?=\.[A-Z][A-Z /&]+\.\.\.)", t)
    keep = [b for b in blocks if not any(k in b.strip()[:40].upper() for k in SKIP_SECTIONS)]
    return issued, "\n".join(keep)[:7000]


def ask_llm(text, issued_local, day, station):
    if not OLLAMA_URL:
        raise RuntimeError("OLLAMA_URL нет в .env — адрес Ollama в домашней сети")
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": OLLAMA_MODEL, "stream": False, "think": False, "format": "json", "options": {"temperature": 0},
        "messages": [{"role": "user", "content": PROMPT.format(issued=issued_local, day=day, station=station, text=text)}]},
        timeout=300)
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def tri(v):
    return None if v is None else int(bool(v))


def process(conn, city, d):
    cfg = OBS_CITIES[city]
    wfo, station = WFO[city]
    tz = ZoneInfo(cfg["tz"])
    before = datetime(d.year, d.month, d.day, *DECISION, tzinfo=tz).astimezone(timezone.utc)
    issued, text = latest_afd(wfo, before)
    if issued is None:
        return False
    day = d.strftime("%A %B %-d, %Y")
    res = ask_llm(text, issued.astimezone(tz).strftime("%Y-%m-%d %H:%M"), day, station)
    hf = res.get("high_f")
    try:
        hf = float(hf) if hf is not None else None
    except (TypeError, ValueError):
        hf = None
    conn.execute(
        "INSERT OR REPLACE INTO afd_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (city, d.isoformat(), wfo, issued.isoformat(), hf, res.get("vs_guidance"), tri(res.get("sea_breeze")),
         tri(res.get("clouds_limit_heating")), tri(res.get("rain_today")), tri(res.get("front_today")),
         res.get("confidence"), json.dumps(res)))
    conn.commit()
    return True


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    ensure_schema(conn)
    done = {(r[0], r[1]) for r in conn.execute("SELECT city, local_date FROM afd_signals")}
    n = 0
    for city in WFO:
        tz = ZoneInfo(OBS_CITIES[city]["tz"])
        now = datetime.now(tz)
        last = now.date() if (now.hour, now.minute) >= DECISION else now.date() - timedelta(days=1)
        d = HISTORY_START if "--backfill" in sys.argv else last - timedelta(days=2)
        while d <= last:
            if (city, d.isoformat()) not in done:
                try:
                    if process(conn, city, d):
                        n += 1
                except (requests.RequestException, ValueError, KeyError) as e:
                    print(f"{city} {d}: ошибка — {e}", file=sys.stderr)
            d += timedelta(days=1)
        print(f"{city}: готово", flush=True)
    print(f"обработано разборов: {n}")
    conn.close()


if __name__ == "__main__":
    main()
