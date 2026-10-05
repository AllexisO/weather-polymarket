"""
Исторические стаканы погодных вариантов из Falcon API для проверки бота-мейкера на истории (2026-09-30, Alex оплатил $10 =
20 000 кредитов; 1 запрос = 2 кредита, 200 снимков ~ раз в 1-2 мин). Ключ PMA_TOKEN (вход Alex, только чтение).
Выборка: N_DAYS маркет-дней 19.08-27.09 (равномерно, зерно 7), в каждом 3 самых торгуемых варианта (по poly_trades);
окно — с 12:00 местного накануне до 18:00 местного в день маркета (как работает живой бот). Жёсткий предел MAX_REQ
запросов. Каждый вариант — файл data/research/falcon/book/<condition_id>.json.gz: [(ts, [[цена, объём] заявок топ-5],
[[цена, объём] предложений топ-5]), ...] по токену «да». Уже скачанные не перекачиваются. Только чтение рабочей базы (копия).
"""
import gzip
import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES

URL = "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
OUT = Path("/data/research/falcon/book")
N_DAYS, PER_DAY, MAX_PAGES = 2000, 3, 7
MAX_REQ = int(os.environ.get("MAX_REQ", "9600"))
conn = sqlite3.connect("/data/research/research.sqlite3")
state = {"req": 0}
LOCK = threading.Lock()


def post(token, s_ms, e_ms, offset):
    for attempt in range(5):
        try:
            r = requests.post(URL, headers={"Authorization": f"Bearer {os.environ['PMA_TOKEN']}"}, timeout=60,
                              json={"agent_id": 572, "params": {"token_id": token, "start_time": str(s_ms), "end_time": str(e_ms)},
                                    "pagination": {"limit": 200, "offset": offset}, "formatter_config": {"format_type": "raw"}})
            with LOCK:
                state["req"] += 1
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(3 * (attempt + 1))
    return None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    days = conn.execute("""SELECT DISTINCT city, local_date FROM poly_market_final WHERE local_date BETWEEN '2026-08-19' AND '2026-09-27'""").fetchall()
    days = [d for d in days if d[0] in OBS_CITIES]
    random.Random(7).shuffle(days)
    days = days[:N_DAYS]  # случайный порядок: при пределе запросов обе половины периода представлены поровну
    # 30.09: один проход по сделкам вместо запроса на каждый маркет-день (тот перебирал 2.8 млн строк 1700 раз)
    top = {}
    for city, ld, cid, token, n in conn.execute("""SELECT f.city, f.local_date, t.condition_id, t.asset, COUNT(*) FROM poly_trades t
                                                  JOIN poly_market_final f ON f.condition_id = t.condition_id
                                                  WHERE t.outcome = 'Yes' AND f.local_date BETWEEN '2026-08-19' AND '2026-09-27'
                                                  GROUP BY t.condition_id, t.asset"""):
        top.setdefault((city, ld), []).append((n, cid, token))
    jobs = []
    for city, ld in days:
        tz = ZoneInfo(OBS_CITIES[city]["tz"])
        s_ = datetime.fromisoformat(ld).replace(tzinfo=tz) - timedelta(hours=12)
        e_ = datetime.fromisoformat(ld).replace(tzinfo=tz) + timedelta(hours=18)
        for n, cid, token in sorted(top.get((city, ld), []), reverse=True)[:PER_DAY]:
            if not (OUT / f"{cid}.json.gz").exists():
                jobs.append((cid, token, city, ld, s_, e_))
    print(f"осталось скачать вариантов: {len(jobs)}", flush=True)
    cnt = {"done": 0, "snap": 0}

    def work(job):
        cid, token, city, ld, s_, e_ = job
        if state["req"] >= MAX_REQ:
            return
        snaps = []
        for page in range(MAX_PAGES):
            d = post(token, int(s_.timestamp() * 1000), int(e_.timestamp() * 1000), 200 * page)
            if d is None:
                return  # не записываем — докачается в следующий раз
            for x in (d.get("data") or {}).get("results") or []:
                try:
                    bids = sorted(([float(b["price"]), float(b["size"])] for b in json.loads(x["bids"])), reverse=True)[:5]
                    asks = sorted([float(a["price"]), float(a["size"])] for a in json.loads(x["asks"]))[:5]
                    snaps.append((datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).timestamp(), bids, asks))
                except (ValueError, KeyError, TypeError):
                    continue
            if not (d.get("pagination") or {}).get("has_more"):
                break
        snaps.sort()
        with gzip.open(OUT / f"{cid}.json.gz", "wt") as fh:
            json.dump({"city": city, "local_date": ld, "token": token, "snaps": snaps}, fh)
        with LOCK:
            cnt["done"] += 1; cnt["snap"] += len(snaps)
            if cnt["done"] % 200 == 0:
                print(f"вариантов {cnt['done']}, снимков {cnt['snap']}, запросов {state['req']} (≈ {2 * state['req']} кредитов)", flush=True)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(20) as ex:
        list(ex.map(work, jobs))
    summary(cnt["done"], cnt["snap"])


def summary(done, n_snap):
    print(f"ИТОГ: вариантов {done}, снимков {n_snap}, запросов {state['req']} (≈ {2 * state['req']} кредитов)", flush=True)


main()
