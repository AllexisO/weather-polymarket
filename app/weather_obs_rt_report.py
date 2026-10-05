"""
Отчёт кошелька obs_rt (2026-10-01): (1) задержка «время замера → мы увидели сводку» по станциям и источникам
(metar_seen_src, источники *_rt); (2) прямое сравнение с HighTempTation — по каждой его покупке «нет» на вариант, уже
мёртвый по сводке METAR: через сколько минут после замера купил он и через сколько увидели ту же сводку мы.
Чтение только. Запуск: docker compose run --rm collector weather_obs_rt_report.py [дней, по умолчанию 1]
"""
import collections
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from weather_cities import OBS_CITIES
from weather_edge import parse_bucket

MAIN_DB = Path(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"))
HTT = "0x6011655c4afb76f36dd1b08a137a1ba73466b31e"   # HighTempTation, рейтинг погоды Polymarket


def main():
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 1
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    c = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True, timeout=30)
    ic = {v["icao"]: k for k, v in OBS_CITIES.items()}
    # первая встреча каждой сводки каждым источником; «лучший» — минимум по источникам
    first = collections.defaultdict(dict)
    for icao, o, src, fs in c.execute("""SELECT icao, obs_time_utc, source, MIN(first_seen_utc) FROM metar_seen_src
                                         WHERE first_seen_utc >= ? AND source LIKE '%\\_rt' ESCAPE '\\' GROUP BY 1, 2, 3""", (since,)):
        lag = (datetime.fromisoformat(fs) - datetime.fromisoformat(o)).total_seconds() / 60
        if -10 < lag < 60:
            first[(icao, o)][src] = lag
    by = collections.defaultdict(lambda: collections.defaultdict(list))
    for (icao, _), d in first.items():
        for s, v in d.items():
            by[icao][s].append(v)
        by[icao]["лучший"].append(min(d.values()))
    print(f"obs_rt: задержка от времени замера до того, как увидели сводку (медиана, мин), с {since[:16]}")
    for icao in sorted(by, key=lambda i: statistics.median(by[i]["лучший"])):
        d = by[icao]
        print(f"  {icao} {ic.get(icao, '?'):14s} лучший {statistics.median(d['лучший']):5.1f} (n={len(d['лучший'])})  "
              + "  ".join(f"{s} {statistics.median(v):.1f}" for s, v in d.items() if s != "лучший"))
    # сравнение с HighTempTation
    try:
        acts = requests.get("https://data-api.polymarket.com/activity", params={"user": HTT, "type": "TRADE", "limit": 500}, timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        print(f"HighTempTation: data-api недоступен — {e}")
        return
    met = collections.defaultdict(list)
    for icao, o, fs, t in c.execute("""SELECT icao, obs_time_utc, MIN(first_seen_utc), MAX(temp_c) FROM metar_seen_src
                                       WHERE obs_time_utc >= ? GROUP BY icao, obs_time_utc""", (since,)):
        if t is not None:
            met[icao].append((datetime.fromisoformat(o).timestamp(), datetime.fromisoformat(fs).timestamp(), t))
    slug_city = {v["poly_slug"]: k for k, v in OBS_CITIES.items()}
    t0 = datetime.fromisoformat(since).timestamp()
    diffs = []
    print("\nHighTempTation: покупка «нет» на мёртвый вариант против нашего первого взгляда на ту же сводку")
    for x in acts:
        if (x.get("side") != "BUY" or x.get("outcome") != "No" or x.get("timestamp", 0) < t0
                or not (x.get("slug") or "").startswith("highest-temperature-in-")):
            continue
        city = slug_city.get(x["slug"][len("highest-temperature-in-"):].rsplit("-on-", 1)[0])
        rng = parse_bucket(x.get("title") or "")
        if not city or not rng:
            continue
        cfg = OBS_CITIES[city]
        val = (lambda t: round(t * 9 / 5 + 32)) if cfg["unit"] == "fahrenheit" else (lambda t: round(t))
        kills = [(o, fs) for o, fs, t in met.get(cfg["icao"], []) if val(t) > rng[1] and o <= x["timestamp"] + 60]
        if not kills:
            continue
        o, fs = max(kills)
        his, ours = (x["timestamp"] - o) / 60, (fs - o) / 60
        diffs.append(ours - his)
        print(f"  {city:12s} {rng[0]}..{rng[1]} по {x['price']:.3f}: сводка {datetime.fromtimestamp(o, timezone.utc):%d.%m %H:%M}Z, "
              f"он через {his:5.1f} мин, мы увидели через {ours:5.1f} → {'мы раньше' if ours < his else 'он раньше'} на {abs(ours - his):.1f}")
    if diffs:
        print(f"  итого {len(diffs)}: медиана нашего отставания {statistics.median(diffs):+.1f} мин, мы раньше в {sum(d < 0 for d in diffs)}")
    else:
        print("  его покупок на мёртвые варианты за период нет")
    race(c, since)



def race(c, since):
    """02.10: гонка по секундам. Каждая наша попытка (obs_race: момент сводки, стакан в памяти, заявка через lat с) против
    настоящих сделок маркета: когда начали и когда закончили забирать «нет» не дороже 95¢, и сколько ушло после нашего прихода."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'obs_race'").fetchone():
        print("\nгонка по секундам: попыток ещё нет")
        return
    rows = c.execute("""SELECT city, bucket_lo, bucket_hi, obs_time_utc, src, t_seen, t_book, lat, depth_before, condition_id
                        FROM obs_race WHERE t_seen >= ? AND condition_id IS NOT NULL ORDER BY t_seen""",
                     (datetime.fromisoformat(since).timestamp(),)).fetchall()
    print(f"\nгонка по секундам (с {since[:16]}): попыток {len(rows)}")
    won = tot = 0.0
    for city, lo, hi, o, src, ts, tb, lat, dep, cid in rows:
        t0 = datetime.fromisoformat(o).timestamp()
        arrive = (tb or ts) + (lat or 0)
        try:
            tr = requests.get("https://data-api.polymarket.com/trades", params={"market": cid, "limit": 500}, timeout=30).json()
        except (requests.RequestException, ValueError):
            continue
        cheap = sorted((t["timestamp"], (t["price"] if t["outcome"] == "No" else 1 - t["price"]) * t["size"]) for t in tr
                       if t["timestamp"] >= t0 and (t["price"] if t["outcome"] == "No" else 1 - t["price"]) <= 0.95)
        after = sum(v for t, v in cheap if t >= arrive)
        tot += sum(v for _, v in cheap)
        won += after
        first = f"{cheap[0][0] - t0:5.0f}" if cheap else "    —"
        last = f"{cheap[-1][0] - t0:5.0f}" if cheap else "    —"
        print(f"  {city:12s} {lo}..{hi} сводка {o[5:16]} ({src}): мы увидели +{ts - t0:4.0f} с, заявка дошла бы +{arrive - t0:4.0f} с; "
              f"дешёвое «нет» забирали с +{first} по +{last} с, ${sum(v for _, v in cheap):.0f}; после нашего прихода — ${after:.0f}; "
              f"в стакане в момент сводки ${dep or 0:.0f}")
    if rows:
        print(f"  итого: дешёвого «нет» продано ${tot:.0f}, из них после нашего прихода ${won:.0f} ({100 * won / tot if tot else 0:.0f}%)")


if __name__ == "__main__":
    main()
