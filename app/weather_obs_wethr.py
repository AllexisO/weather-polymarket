"""
Кошелёк obs_wethr — как obs_rt, но сводки METAR приходят из Push API wethr.net (2026-10-01, Alex оплатил Professional
на месяц: 5 станций в потоке, одно подключение). Правила покупки те же (weather_obs_rt.buy_dead), отличается только источник.

Почему эти 5 станций (замер 01.10, data/research/wethr_latency_1001.log, 23 ч): у неамериканских станций wethr получает
сводку в ту же минуту, что и наш obs_rt (разница 0.0-0.3 мин — Лондон, Мюнхен, Милан, Мадрид, Москва без выигрыша),
у американских — на ~1.1 мин раньше (wethr 1.1-2.1 мин после замера против наших 2.2-3.3). Пересчёт на стаканы Falcon
(38 дн, вся глубина, верхняя граница): прирост больше всего в Сиэтле, Лос-Анджелесе, Остине, Нью-Йорке, Майами
(data/research/lag_wethr.log).

Как работает: постоянный процесс (крон раз в час в :07, LISTEN_MIN минут, как obs_rt), поток SSE
wethr.net:3443/api/v2/stream (heartbeat раз в 30 с; при обрыве — переподключение с last_event_id). Событие observation:
ASOS-HR (часовая) и ASOS-SPECI — как сводки METAR у obs_rt; ASOS-HFM (5-минутные замеры США) не ставки — только в
fastobs.sqlite3 (fast_obs, source 'wethr_hf') для отчёта о форе. Момент появления сводки — metar_seen_src ('wethr_rt'),
рядом с tgftp_rt/awc_rt — сравнение с obs_rt вживую (weather_obs_rt_report.py).
Запуск: docker compose run --rm -e JOB_TIMEOUT=3600 collector weather_obs_wethr.py
"""
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES
from weather_obs_live import cash, ensure_schema
from weather_obs_rt import FRESH_SEC, Books, Markets, buy_dead, check_watch
from weather_obs_rt import value as value_exact


def value(temp_c, cfg):
    """06.10: поток wethr присылает сводку в целых °C (Майами 05.10 16:53: 32.0 при T-группе 31.7 → посчитали 90°F вместо 89,
    купили «нет» на 88-89°F по 51¢ и проиграли). Целые °C для города в °F — точный °F неизвестен (32°C = 88.7…90.3°F):
    берём нижнюю границу (31.5°C → 89°F). Иногда упустим ставку, но не купим «нет» на вариант, который ещё может выиграть."""
    if cfg["unit"] == "fahrenheit" and float(temp_c).is_integer():
        return value_exact(temp_c - 0.5, cfg)
    return value_exact(temp_c, cfg)

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
FAST_DB = os.path.join(os.path.dirname(DB_PATH), "fastobs.sqlite3")
WALLET = "obs_wethr"
# 03.10: станции — по НАСТОЯЩИМ сделкам за 30 дней: дешёвое «нет» (≤95¢) на убитых вариантах в окне СВОЁМ для каждой станции
# [wethr прислал сводку → NOAA прислал] (задержки — замер 01-02.10): Чикаго $3.1k, Атланта $2.5k, Остин $2.4k, Денвер $0.8k,
# Майами $0.7k = $9.4k (ночная пятёрка с одинаковым окном 45-150 с для всех — $6.9k; прежняя — Сиэтл $0.2k, Лос-Анджелес $0.03k).
# Предел тарифа Professional — 5 станций.
CITIES = ("chicago", "atlanta", "austin", "denver", "miami")
LISTEN_MIN = float(os.environ.get("LISTEN_MIN", "57.5"))
STREAM = "https://wethr.net:3443/api/v2/stream"
REST = "https://wethr.net/api/v2/observations.php"
ICAO_CITY = {OBS_CITIES[c]["icao"]: c for c in CITIES}
# 03.10 (Alex: «не легче их всех использовать?»): поток Professional — только 5 станций, но в тарифе есть обычные запросы
# (60/мин, 5000/сутки). Остальные города США опрашиваем REST «последнее наблюдение» раз в REST_EVERY с — только в окно
# выхода сводки (REST_WIN с после минуты сводки) и днём (DAY_HOURS местного): ~50 запросов/мин в пике, ~1000 в сутки.
# 03.10, проверка на живых сводках 23:51-23:56Z: wethr получил сводки за 62-88 с, но в ответах REST (и latest, и история) они
# появляются только через ~165 с — позже бесплатного NOAA (145-158 с). Для гонки REST бесполезен → опрос выключен (EXTRA пуст);
# все города США через wethr — только поток (тариф $99).
EXTRA = ()
EXTRA_ICAO = {OBS_CITIES[c]["icao"]: c for c in EXTRA}
REST_EVERY = 7.0
REST_WIN = (35, 200)
DAY_HOURS = (9, 20)


def key():
    k = os.environ.get("WETHR_API_KEY")
    if not k:
        sys.exit("WETHR_API_KEY нет в .env")
    return k


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T")).replace(tzinfo=timezone.utc) if s else None


def seed(now, stations=None):
    """Максимум дня по уже вышедшим сводкам (REST, история за сутки; по запросу на станцию). Сводки моложе FRESH_SEC — «новые»."""
    mx, known, fresh = {}, set(), []
    h = {"Authorization": "Bearer " + key()}
    start = (now.timestamp() - 23 * 3600)
    # 05.10: пустой список (EXTRA выключен) раньше значил «все станции потока» — опрос REST получал сводку KATL и падал (KeyError)
    for icao, city in (ICAO_CITY if stations is None else stations).items():
        cfg = OBS_CITIES[city]
        try:
            rows = requests.get(REST, params={"station_code": icao, "start_time": datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                              "end_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, headers=h, timeout=30).json()
        except (requests.RequestException, ValueError) as e:
            print(f"{WALLET}: {city} история недоступна — {e}", file=sys.stderr, flush=True)
            continue
        for x in rows if isinstance(rows, list) else []:
            if not x.get("report_type") or x.get("temperature") is None:   # без типа — 5-минутные замеры (HF), не сводки
                continue
            t = ts(x["observation_time"])
            if now.timestamp() - t.timestamp() < FRESH_SEC:
                fresh.append((icao, t, float(x["temperature"])))
                continue
            known.add((icao, int(t.timestamp())))
            k = (city, t.astimezone(ZoneInfo(cfg["tz"])).date())
            mx[k] = max(mx.get(k, -999), value(float(x["temperature"]), cfg))
    return mx, known, fresh


class RestPoller(threading.Thread):
    """03.10: остальные города США через REST wethr в окна сводок (своё подключение к базе, своя слежка)."""

    def __init__(self, books, mk, end):
        super().__init__(daemon=True)
        self.books, self.mk, self.end = books, mk, end
        try:
            self.mins = json.loads(open(os.path.join(os.path.dirname(DB_PATH), "mm_metar_minutes.json")).read())
        except (OSError, ValueError):
            self.mins = {}
        self.nreq = 0

    def run(self):
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.row_factory = sqlite3.Row
        h = {"Authorization": "Bearer " + key()}
        day_max, known, fresh = seed(datetime.now(timezone.utc), EXTRA_ICAO)
        watch, last = {}, {}

        def handle(icao, t, temp):
            now = datetime.now(timezone.utc)
            try:
                conn.execute("INSERT OR IGNORE INTO metar_seen_src (icao, obs_time_utc, source, first_seen_utc, temp_c) VALUES (?, ?, ?, ?, ?)",
                             (icao, t.isoformat(), "wethr_rest_rt", now.isoformat(), temp))
                conn.commit()
            except sqlite3.OperationalError:
                conn.rollback()
            city = EXTRA_ICAO[icao]
            cfg = OBS_CITIES[city]
            tz = ZoneInfo(cfg["tz"])
            day, v = t.astimezone(tz).date(), value(temp, cfg)
            if day != now.astimezone(tz).date() or v <= day_max.get((city, day), -999):
                day_max[(city, day)] = max(day_max.get((city, day), -999), v)
                return
            prev = day_max.get((city, day), -999)
            day_max[(city, day)] = v
            buy_dead(conn, city, cfg, day, v, prev, t, self.mk.get(city, day), "wethr_rest_rt", wallet=WALLET, books=self.books, watch=watch)

        for icao, t, temp in fresh:
            known.add((icao, int(t.timestamp())))
            handle(icao, t, temp)
        while time.time() < self.end:
            now = datetime.now(timezone.utc)
            sec = now.minute * 60 + now.second
            for icao, city in EXTRA_ICAO.items():
                if not DAY_HOURS[0] <= now.astimezone(ZoneInfo(OBS_CITIES[city]["tz"])).hour < DAY_HOURS[1]:
                    continue
                if not any(REST_WIN[0] <= (sec - m * 60) % 3600 <= REST_WIN[1] for m in self.mins.get(city, [53])):
                    continue
                if time.time() - last.get(icao, 0) < REST_EVERY:
                    continue
                last[icao] = time.time()
                # 03.10: не mode=latest — он отдаёт сводку с опозданием и прячет её за 5-минутным замером (02.10 22:53: wethr
                # получил сводку Сиэтла за 62 с, а latest её так и не показал). История за 20 мин — тоже один запрос.
                try:
                    rows = requests.get(REST, params={"station_code": icao,
                                                      "start_time": datetime.fromtimestamp(time.time() - 1200, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                      "end_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                                        headers=h, timeout=10).json()
                    self.nreq += 1
                except (requests.RequestException, ValueError):
                    continue
                for x in rows if isinstance(rows, list) else []:
                    if not x.get("report_type") or x.get("temperature") is None or not x.get("observation_time"):
                        continue   # 5-минутные замеры (HF) — не сводки
                    t = ts(x["observation_time"])
                    if (icao, int(t.timestamp())) in known:
                        continue
                    known.add((icao, int(t.timestamp())))
                    try:
                        handle(icao, t, float(x["temperature"]))
                    except Exception as e:  # noqa: BLE001 — один город не роняет опрос
                        conn.rollback()
                        print(f"{WALLET}: REST {city} ошибка — {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            try:
                check_watch(conn, self.books, watch, WALLET)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                print(f"{WALLET}: REST слежка — ошибка {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(1)
        conn.close()


def events(last_id, end):
    """Поток SSE: (id, событие, данные). Обрыв → исключение, переподключение снаружи."""
    p = {"stations": ",".join(ICAO_CITY), "api_key": key()}
    if last_id:
        p["last_event_id"] = last_id
    with requests.get(STREAM, params=p, stream=True, timeout=(10, 75), headers={"Accept": "text/event-stream"}) as r:
        r.raise_for_status()
        eid, ev, data = None, None, []
        for line in r.iter_lines(decode_unicode=True):
            if time.time() > end:
                return
            if line is None:
                continue
            if line == "":
                if data:
                    yield eid, ev, "\n".join(data)
                eid, ev, data = eid, None, []
                continue
            if line.startswith(":"):
                continue
            f, _, v = line.partition(":")
            v = v[1:] if v.startswith(" ") else v
            if f == "id":
                eid = v
            elif f == "event":
                ev = v
            elif f == "data":
                data.append(v)


def main():
    from jobmark import mark_alive, single_instance
    single_instance("obs_wethr")
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    fast = sqlite3.connect(FAST_DB, timeout=60)
    fast.execute("""CREATE TABLE IF NOT EXISTS fast_obs (source TEXT, city TEXT, obs_utc TEXT, temp_c REAL, first_seen_utc TEXT,
                    PRIMARY KEY (source, city, obs_utc))""")
    # 02.10 (Alex: «давай»): события «новый максимум дня» wethr — nws (с минутными замерами ASOS между сводками) и wu
    # (только сводки, включая ещё не дошедшие по обычной цепочке). Пока только собираем: проверить, насколько раньше сводки
    # они приходят и совпадают ли с итогом Polymarket (часовые сводки) — порог в docs/PRD.md §9 п.13.
    fast.execute("""CREATE TABLE IF NOT EXISTS wethr_extremes (station TEXT, logic TEXT, value_f INTEGER, value_c INTEGER,
                    prev_f INTEGER, obs_time_utc TEXT, sources TEXT, detail TEXT, received_utc TEXT, event_id TEXT PRIMARY KEY)""")
    now = datetime.now(timezone.utc)
    day_max, known, fresh = seed(now)
    mk, alive = Markets(), {"t": 0}
    no_tokens = []
    for c in CITIES + EXTRA:
        for _rng, m in mk.get(c, now.astimezone(ZoneInfo(OBS_CITIES[c]["tz"])).date()):
            no_tokens.append(json.loads(m["clobTokenIds"])[1])
    books = Books(no_tokens)   # 02.10: как obs_rt — стаканы в памяти, заявка доходит за ORDER_LAT (сравнение двух кошельков честное)
    books.start()
    books.ready.wait(20)
    end = time.time() + LISTEN_MIN * 60
    rest = RestPoller(books, mk, end)   # 03.10: остальные 6 городов США — REST в окна сводок
    if EXTRA:   # 05.10: при пустом EXTRA (сейчас) не запускаем
        rest.start()
    last_id, n_new, n_ev, n_err = None, 0, 0, 0
    watch = {}
    print(f"{WALLET}: старт, станции {', '.join(ICAO_CITY)}, максимумов дня {len(day_max)}, свежих сводок {len(fresh)}", flush=True)

    def handle(icao, t, temp, src):
        nonlocal n_new
        now = datetime.now(timezone.utc)
        try:
            conn.execute("INSERT OR IGNORE INTO metar_seen_src (icao, obs_time_utc, source, first_seen_utc, temp_c) VALUES (?, ?, ?, ?, ?)",
                         (icao, t.isoformat(), src, now.isoformat(), temp))
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()
        if (icao, int(t.timestamp())) in known:
            return
        known.add((icao, int(t.timestamp())))
        n_new += 1
        city = ICAO_CITY[icao]
        cfg = OBS_CITIES[city]
        tz = ZoneInfo(cfg["tz"])
        day = t.astimezone(tz).date()
        v = value(temp, cfg)
        if day != now.astimezone(tz).date() or v <= day_max.get((city, day), -999):
            day_max[(city, day)] = max(day_max.get((city, day), -999), v)
            return
        prev = day_max.get((city, day), -999)
        day_max[(city, day)] = v
        try:
            buy_dead(conn, city, cfg, day, v, prev, t, mk.get(city, day), src, wallet=WALLET, books=books, watch=watch)
        except Exception as e:  # noqa: BLE001 — один город не роняет весь час
            conn.rollback()
            print(f"{WALLET}: {city} ошибка — {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    for icao, t, temp in fresh:   # вышли во время перезапуска
        handle(icao, t, temp, "wethr_rest")
    while time.time() < end:
        try:
            for eid, ev, data in events(last_id, end):
                last_id = eid or last_id
                mark_alive("weather_obs_wethr", alive)
                try:
                    check_watch(conn, books, watch, WALLET)   # 03.10: слежка за мёртвыми вариантами (события идут чаще раза в 30 с)
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    print(f"{WALLET}: слежка — ошибка {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                if ev == "new_high":
                    try:
                        x = json.loads(data)
                        fast.execute("INSERT OR IGNORE INTO wethr_extremes VALUES (?,?,?,?,?,?,?,?,?,?)",
                                     (x.get("station_code"), x.get("logic"), x.get("value_f"), x.get("value_c"), x.get("prev_value_f"),
                                      x.get("observation_time_utc"), json.dumps(x.get("sources")), json.dumps(x.get("source_detail")),
                                      datetime.now(timezone.utc).isoformat(), x.get("id") or eid))
                        fast.commit()
                    except (ValueError, sqlite3.OperationalError):
                        fast.rollback()
                    continue
                if ev != "observation":
                    continue
                try:
                    x = json.loads(data)
                except ValueError:
                    continue
                icao, temp = x.get("station_code"), x.get("temperature_celsius")
                if icao not in ICAO_CITY or temp is None or not x.get("observation_time_utc"):
                    continue
                n_ev += 1
                t = ts(x["observation_time_utc"])
                if x.get("product") == "ASOS-HFM":   # 5-минутный замер — не сводка, только для отчёта о форе
                    try:
                        fast.execute("INSERT OR IGNORE INTO fast_obs VALUES (?, ?, ?, ?, ?)",
                                     ("wethr_hf", ICAO_CITY[icao], t.isoformat(), float(temp), datetime.now(timezone.utc).isoformat()))
                        fast.commit()
                    except sqlite3.OperationalError:
                        fast.rollback()
                    continue
                handle(icao, t, float(temp), "wethr_rt")
        except (requests.RequestException, ValueError) as e:
            n_err += 1
            print(f"{WALLET}: поток оборвался ({type(e).__name__}: {str(e)[:120]}) — переподключение", file=sys.stderr, flush=True)
            time.sleep(min(30, 3 * n_err))
    if rest.is_alive():
        rest.join(timeout=30)
    print(f"{WALLET}: за запуск событий {n_ev}, новых сводок {n_new}, обрывов {n_err}, запросов REST {rest.nreq}, "
          f"баланс ${cash(conn, WALLET):.2f}", flush=True)
    conn.close()
    fast.close()


if __name__ == "__main__":
    main()
