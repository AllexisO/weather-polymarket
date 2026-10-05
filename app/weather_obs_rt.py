"""
Кошелёк obs_rt — «по живым замерам», но быстро (2026-10-01, решение Alex). Разбор HighTempTation (+$15.8k/мес): 84% его
денег — «нет» на варианты, уже мёртвые по только что вышедшей сводке METAR, через 1.3 мин (медиана) после времени
замера. По всему рынку покупки «нет» < 97¢ на мёртвый вариант: 0-2 мин ≈ $1 000/день прибыли, 2-3 мин ≈ $145, позже
10 мин ≈ 0. Наш obs видит сводку через ~6 мин (крон раз в 2 мин + запуск контейнера), а сам NOAA (tgftp) при частом
опросе — через ~2.5 мин (10% — через 0.6). Отставание в основном наше — этот кошелёк его убирает.

Как работает: постоянный процесс (крон раз в час в :07 — в :05-:15 почти нет плановых сводок; работает LISTEN_MIN минут), каждые POLL_SEC секунд
проверяет файлы NOAA tgftp по станциям (условный запрос: неизменённый файл — пустой ответ 304; только города, где сейчас 08-20),
для Польши — ещё лента IMGW (скорость против NOAA копим), раз в AWC_SEC — ещё API aviationweather (в гонке источников был первым в 5 из 13 сводок). Новая сводка нашей станции → максимум дня (как в obs: °F — из
T-группы, °C — до целого); вырос → по каждому варианту ниже максимума покупаем «нет» по ЖИВОМУ стакану (polyexec), не
дороже 1 − MIN_BID, $2. Маркеты дня загружены заранее (без запроса Gamma в момент сводки). Одна попытка на вариант в день
(как obs); итог считает weather_obs_live.settle (общая таблица paper_obs_trades). Задержка видна по каждой ставке:
placed_at − obs_time_utc; момент появления каждой сводки — metar_seen_src, источник 'tgftp_rt' / 'awc_rt'.
Запуск: docker compose run --rm -e JOB_TIMEOUT=3600 collector weather_obs_rt.py
"""
import json
import math
import os
import threading
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from polyexec import fee_for, fee_params, simulate_buy, trading_stopped
from weather_cities import OBS_CITIES
from weather_edge import GAMMA, month_day_year_slug, parse_bucket
from weather_obs_live import MIN_BID, MIN_FILL, STAKE, cash, ensure_schema, parse_metar_temp

DB_PATH = Path(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"))
WALLET = "obs_rt"
LISTEN_MIN = float(os.environ.get("LISTEN_MIN", "57.5"))   # с :07 до :04:30 следующего часа
POLL_SEC = 1          # 02.10: цикл раз в секунду; станция в окне сводки — каждую секунду, вне окна — раз в SLOW_SEC
SLOW_SEC = 5
WIN_MIN = 7           # окно сводки: от минуты по расписанию (mm_metar_minutes.json) + 7 мин
WATCH_SEC = 600     # 03.10: после сводки ещё 10 мин следим за мёртвым вариантом — дешёвое «нет» появляется снова
ORDER_LAT = 0.5       # 02.10: доставка нашей заявки до биржи (было 2 с в polyexec — для гонки на секунды слишком грубо)
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
AWC_SEC = 5   # 01.10: в гонке источников aviationweather был первым в 5 из 13 сводок — опрашиваем так же часто, как tgftp
TGFTP = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/{}.TXT"
METAR_API = "https://aviationweather.gov/api/data/metar"
ICAO_CITY = {cfg["icao"]: c for c, cfg in OBS_CITIES.items()}
RE_HEAD = re.compile(r"^(?:METAR |SPECI )?([A-Z0-9]{4}) (\d{2})(\d{2})(\d{2})Z")


def value(temp_c, cfg):
    """Значение для маркета — как в weather_obs_live.observed_max_today."""
    return round(temp_c * 9 / 5 + 32) if cfg["unit"] == "fahrenheit" else round(temp_c)


class Feed:
    """Файлы NOAA tgftp по станциям (stations/XXXX.TXT) с условным запросом If-Modified-Since: неизменённый — ответ 304
    без тела. (Общий файл часа cycles/HHZ.TXT переписывается целиком, а не дописывается — докачка по байтам не работает.)
    Опрашиваем только города, где сейчас 08-20 местного — максимум дня бывает днём."""

    def __init__(self):
        self.lm = {}
        self.last = {}
        self.s = requests.Session()
        try:   # минуты сводок по городам (собирает weather_mm_paper.py раз в сутки)
            self.mins = json.loads((DB_PATH.parent / "mm_metar_minutes.json").read_text())
        except (OSError, ValueError):
            self.mins = {}

    def in_window(self, icao, now):
        """02.10: сейчас окно выхода сводки этой станции (минута по расписанию + WIN_MIN). Нет расписания — всегда окно."""
        mins = self.mins.get(ICAO_CITY.get(icao, ""), [])
        return not mins or any((now.minute - m) % 60 <= WIN_MIN for m in mins)

    def due(self, icao, now):
        """Пора опрашивать: в окне сводки — каждую секунду, вне окна — раз в SLOW_SEC."""
        if self.in_window(icao, now) or time.time() - self.last.get(icao, 0) >= SLOW_SEC:
            self.last[icao] = time.time()
            return True
        return False

    def one(self, icao):
        h = {"If-Modified-Since": self.lm[icao]} if icao in self.lm else {}
        try:
            r = self.s.get(TGFTP.format(icao), headers=h, timeout=8)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        self.lm[icao] = r.headers.get("Last-Modified", "")
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None
        m = RE_HEAD.match(lines[1].strip())
        return (m.group(1), m.group(2), m.group(3), m.group(4), lines[1].strip()) if m else None

    def poll(self, now):
        act = [cfg["icao"] for cfg in OBS_CITIES.values() if 8 <= now.astimezone(ZoneInfo(cfg["tz"])).hour < 20
               and self.due(cfg["icao"], now)]
        with ThreadPoolExecutor(8) as ex:
            return [x for x in ex.map(self.one, act) if x]


class Books(threading.Thread):
    """02.10: стаканы «нет» всех вариантов дня — в памяти, по живому потоку Polymarket (как у бота-мейкера). В момент
    сводки стакан уже есть: не тратим секунды на запрос. Стакан «нет» биржа отдаёт с зеркалом заявок «да»."""

    def __init__(self, tokens):
        super().__init__(daemon=True)
        self.tokens = list(tokens)
        self.asks = {}       # токен «нет» -> {цена: долей}
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def run(self):
        import websocket
        while True:
            try:
                ws = websocket.create_connection(WS_URL, timeout=30)
                ws.send(json.dumps({"assets_ids": self.tokens, "type": "market"}))
                ping = time.time()
                while True:
                    if time.time() - ping > 10:
                        ws.send("PING")
                        ping = time.time()
                    raw = ws.recv()
                    if raw and raw != "PONG":
                        self.handle(raw)
            except Exception as e:  # noqa: BLE001 — обрыв: переподключаемся, а покупки пока идут по запросу стакана
                print(f"{WALLET}: поток стаканов оборвался ({type(e).__name__}) — переподключаюсь", flush=True)
                time.sleep(3)

    def handle(self, raw):
        try:
            data = json.loads(raw)
        except ValueError:
            return
        with self.lock:
            for d in data if isinstance(data, list) else [data]:
                et = d.get("event_type")
                if et == "book" and d.get("asset_id") in self.wanted:
                    self.asks[d["asset_id"]] = {float(x["price"]): float(x["size"]) for x in d.get("asks", []) if float(x["size"]) > 0}
                    self.ready.set()
                elif et == "price_change":
                    for c in d.get("price_changes", []):
                        tok = c.get("asset_id")
                        if tok in self.asks and c.get("side") == "SELL":
                            p, sz = float(c["price"]), float(c["size"])
                            if sz > 0:
                                self.asks[tok][p] = sz
                            else:
                                self.asks[tok].pop(p, None)

    @property
    def wanted(self):
        if not hasattr(self, "_w"):
            self._w = set(self.tokens)
        return self._w

    def get(self, token):
        with self.lock:
            a = self.asks.get(token)
            return None if a is None else sorted(a.items())


def fill_from_asks(market, asks, budget, max_price):
    """Исполнение покупки по готовому стакану (как simulate_buy, но без запроса): доли, цена, комиссия, минимум долей."""
    rate, exponent = fee_params(market)
    min_size = float(market.get("orderMinSize") or 5)
    out = {"shares": 0.0, "cost": 0.0, "fee": 0.0, "avg": None, "reason": None}
    if asks and asks[0][0] <= max_price:
        p0 = asks[0][0]
        budget = max(budget, min_size * (p0 + rate * (p0 * (1 - p0)) ** exponent) * 1.001)
    left = budget
    for price, size in asks:
        if price > max_price:
            break
        per = price + rate * (price * (1 - price)) ** exponent
        take = min(size, left / per)
        if take <= 0:
            break
        out["shares"] += take
        out["cost"] += take * price
        out["fee"] += fee_for(take, price, rate, exponent)
        left -= take * per
    if out["shares"] > 0:
        out["avg"] = out["cost"] / out["shares"]
    if not asks:
        out["reason"] = "продавцов в стакане нет"
    elif out["shares"] == 0:
        out["reason"] = f"продавали от {asks[0][0] * 100:.1f}¢, а выгодно — не дороже {max_price * 100:.1f}¢"
    elif out["shares"] < min_size:
        out["reason"] = f"по выгодной цене продавали только {out['shares']:.1f} долей, а минимум — {min_size:.0f}"
        out.update(shares=0.0, cost=0.0, fee=0.0, avg=None)
    return out


IMGW_URL = "https://aviation-api.imgw.pl/data/last"
IMGW_HEAD = {"Origin": "https://awiacja.imgw.pl", "Referer": "https://awiacja.imgw.pl/", "User-Agent": "Mozilla/5.0"}


def imgw(sess, now):
    """Сводки польских аэропортов от IMGW (авиационная служба Польши). 01.10: за 12:00 — на 36 с раньше NOAA, за 13:30 —
    на 1.8 мин позже; кто быстрее в среднем — копим (metar_seen_src, 'imgw_rt'). Кошелёк берёт сводку, пришедшую первой."""
    ours = [i for i in ICAO_CITY if i.startswith("EP") and 8 <= now.astimezone(ZoneInfo(OBS_CITIES[ICAO_CITY[i]]["tz"])).hour < 20]
    if not ours:
        return []
    try:
        d = sess.get(IMGW_URL, params={"params": "metar", "format": "json", "count": 1}, headers=IMGW_HEAD, timeout=8).json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for icao in ours:
        for x in ((d.get(icao) or {}).get("metars") or {}).get("sa", {}).get("pl", [])[:1]:
            raw = (x.get("message") or "").replace("METAR ", "").replace("SPECI ", "").rstrip("=").strip()
            m = RE_HEAD.match(raw)
            if m:
                out.append((m.group(1), m.group(2), m.group(3), m.group(4), raw))
    return out


SWOB = "https://dd.weather.gc.ca/{d}/WXO-DD/observations/swob-ml/{d}/{icao}/"
MGM_URL = "https://servis.mgm.gov.tr/web/sondurumlar"
MGM_HEAD = {"Origin": "https://www.mgm.gov.tr", "Referer": "https://www.mgm.gov.tr/", "User-Agent": "Mozilla/5.0"}
MGM_ST = {"LTAC": 17128, "LTFM": 17058}   # номера станций MGM на аэропортах Анкары и Стамбула
NAT_SEC = 10   # вне окна сводки; в окне — раз в секунду


def active(icao, now):
    return icao in ICAO_CITY and 8 <= now.astimezone(ZoneInfo(OBS_CITIES[ICAO_CITY[icao]]["tz"])).hour < 20


def canada(sess, now, done):
    """Торонто: файлы наблюдений Environment Canada (SWOB-ML, та же ручная сводка, что METAR, с десятыми). Гонка 01.10:
    сводка за 15:00Z — через 2.0 мин после замера, у NOAA — через 7.6; HighTempTation покупает в :00-:01.
    Десятые → целое как в METAR с запасом: ровно x.5 считаем вниз (19.5 → 19) — не убиваем вариант по спорному значению."""
    if not active("CYYZ", now):
        return []
    d = now.strftime("%Y%m%d")
    try:
        names = re.findall(r'href="(\d{4}-\d\d-\d\d-(\d\d)(\d\d)-CYYZ-[A-Z]+-swob\.xml)"', sess.get(SWOB.format(d=d, icao="CYYZ"), timeout=8).text)
    except requests.RequestException:
        return []
    out = []
    for name, hh, mm in names:
        if name in done:
            continue
        t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if (now - t).total_seconds() > 900:   # старые файлы дня (первый проход) — не качаем
            done.add(name)
            continue
        try:
            m = re.search(r'name="air_temp" uom="[^"]*" value="(-?[\d.]+)"', sess.get(SWOB.format(d=d, icao="CYYZ") + name, timeout=8).text)
        except requests.RequestException:
            continue
        done.add(name)
        if m:
            out.append(("CYYZ", t, math.ceil(float(m.group(1)) - 0.5), "swob_rt"))
    return out


def mgm(sess, now):
    """Анкара и Стамбул: служба погоды Турции (MGM) отдаёт готовую сводку METAR аэропорта. Гонка 01.10: через 1.4-1.5 мин
    после замера, у NOAA — через 5.2; HighTempTation покупает через 1.4."""
    out = []
    for icao, no in MGM_ST.items():
        if not active(icao, now):
            continue
        try:
            raw = sess.get(MGM_URL, params={"istno": no}, headers=MGM_HEAD, timeout=8).json()[0].get("rasatMetar") or ""
        except (requests.RequestException, ValueError, IndexError, KeyError):
            continue
        m = RE_HEAD.match(raw.strip())
        if m and m.group(1) == icao:
            t = obs_time(m.group(2), m.group(3), m.group(4), now)
            v = parse_metar_temp(" " + raw.rstrip("= ") + " ")
            if t is not None and v is not None:
                out.append((icao, t, v, "mgm_rt"))
    return out


def obs_time(day, hh, mm, now):
    """Время замера из «DDHHMMZ»: этот месяц, а если день больше сегодняшнего — прошлый (сводка конца месяца)."""
    base = now if int(day) <= now.day else (now.replace(day=1) - timedelta(days=1))
    try:
        return base.replace(day=int(day), hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except ValueError:
        return None


class Markets:
    """Маркеты дня по городам — заранее, обновление раз в REFRESH секунд и при смене даты."""
    REFRESH = 600

    def __init__(self):
        self.cache = {}

    def get(self, city, day):
        k = (city, day)
        hit = self.cache.get(k)
        if hit and time.time() - hit[0] < self.REFRESH:
            return hit[1]
        slug = f"highest-temperature-in-{OBS_CITIES[city]['poly_slug']}-on-{month_day_year_slug(day)}"
        try:
            ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
        except (requests.RequestException, ValueError):
            return hit[1] if hit else []
        ms = []
        for m in (ev[0]["markets"] if ev else []):
            rng = parse_bucket(m["question"])
            if rng is not None and not m.get("closed"):
                ms.append((rng, m))
        self.cache[k] = (time.time(), ms)
        return ms


RACE_SQL = """CREATE TABLE IF NOT EXISTS obs_race (wallet TEXT, city TEXT, local_date TEXT, bucket_lo REAL, bucket_hi REAL,
               obs_time_utc TEXT, src TEXT, t_seen REAL, t_book REAL, lat REAL, ask_before REAL, depth_before REAL,
               ask_after REAL, depth_after REAL, shares REAL, from_ws INTEGER, condition_id TEXT, PRIMARY KEY (wallet, city, local_date, bucket_lo))"""


def depth(asks, cap):
    """Сколько долларов «нет» продавали не дороже cap."""
    return sum(p * s for p, s in asks or [] if p <= cap + 1e-9)


def buy_dead(conn, city, cfg, day, mx, prev, t_obs, markets, src, wallet=WALLET, books=None, watch=None):
    """wallet — 01.10: та же покупка и для obs_wethr (источник — Push API wethr.net).
    books — 02.10: стаканы в памяти (живой поток); заявка «доходит» через ORDER_LAT с. Каждая попытка — в obs_race
    (время с точностью до мс, стакан в момент сводки и после задержки) — для сверки гонки с настоящими сделками."""
    t_seen = time.time()
    seen = {r[0] for r in conn.execute("SELECT bucket_lo FROM paper_obs_trades WHERE wallet = ? AND city = ? AND local_date = ?",
                                       (wallet, city, day.isoformat()))}
    # только варианты, которые убила ЭТА сводка (были живы при прежнем максимуме prev)
    dead = [(rng, m) for rng, m in markets if prev <= rng[1] < mx and rng[0] not in seen]
    if not dead or trading_stopped():
        return
    now = datetime.now(timezone.utc)

    def sim(x):
        rng, m = x
        tok = json.loads(m["clobTokenIds"])[1]
        before = books.get(tok) if books is not None else None
        if before is not None:
            t_book = time.time()
            time.sleep(ORDER_LAT)
            after = books.get(tok) or []
            r = fill_from_asks(m, after, STAKE, 1 - MIN_BID)
            r.update(book=json.dumps({"before": before[:5], "after": after[:5]}), before=before, after=after, t_book=t_book, ws=1)
            return rng, m, r
        try:
            return rng, m, simulate_buy(m, json.loads(m["clobTokenIds"])[1], STAKE, 1 - MIN_BID)
        except (requests.RequestException, ValueError, KeyError) as e:
            return rng, m, {"cost": 0.0, "shares": 0.0, "fee": 0.0, "avg": None, "book": None, "reason": f"ошибка стакана: {e}"}

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(sim, dead))
    lag = (now - t_obs).total_seconds() / 60
    cap = 1 - MIN_BID
    try:
        conn.execute(RACE_SQL)
        for rng, m, r in res:
            b = r.get("before")
            if b is None:
                try:
                    b = json.loads(r.get("book") or "{}").get("before") or []
                except ValueError:
                    b = []
            a = r.get("after", b)
            conn.execute("INSERT OR IGNORE INTO obs_race VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (wallet, city, day.isoformat(), rng[0], rng[1], t_obs.isoformat(), src, t_seen, r.get("t_book"),
                          ORDER_LAT if r.get("ws") else None, b[0][0] if b else None, depth(b, cap), a[0][0] if a else None,
                          depth(a, cap), r["shares"], int(bool(r.get("ws"))), m.get("conditionId")))
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
    for rng, m, r in res:
        if cash(conn, wallet) < STAKE:
            print(f"{wallet}: денег нет")
            break
        filled = r["cost"] >= MIN_FILL
        if not filled and watch is not None and books is not None:   # 03.10: не купили сразу — следим WATCH_SEC
            watch[(wallet, city, day.isoformat(), rng[0])] = {"cfg": cfg, "day": day, "mx": mx, "t_obs": t_obs, "rng": rng, "m": m,
                                                             "tok": json.loads(m["clobTokenIds"])[1], "src": src,
                                                             "until": time.time() + WATCH_SEC}
        if not filled:   # вариант уже «умер» в цене до сводки (стакан пуст или «нет» ≥ 99¢) — не попытка, не пишем
            try:
                asks = json.loads(r["book"] or "{}").get("before") or []
            except ValueError:
                asks = []
            if not asks or min(a[0] for a in asks) >= 0.99:
                print(f"{wallet}: {city} {day} сводка {t_obs:%H:%M}Z → максимум {mx}, вариант {rng[0]}..{rng[1]} мёртв; через {lag:.1f} мин "
                      f"— уже нечего покупать ({'стакан пуст' if not asks else f'«нет» от {min(a[0] for a in asks)*100:.1f}¢'})", flush=True)
                continue
        reason = None if filled else (
            f"через {lag:.1f} мин после замера ({src}) ставки против уже не было: "
            + (r["reason"] or f"можно было купить меньше чем на ${MIN_FILL:.0f}"))
        conn.execute(
            """INSERT OR IGNORE INTO paper_obs_trades
               (wallet, city, local_date, unit, bucket_lo, bucket_hi, obs_max, obs_time_utc, placed_at, yes_bid,
                price, stake, status, reason, shares, fee, book_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (wallet, city, day.isoformat(), cfg["unit"], rng[0], rng[1], mx, t_obs.isoformat(), now.isoformat(),
             float(m.get("bestBid") or 0), r["avg"], r["cost"] if filled else 0.0, "open" if filled else "nofill",
             reason, r["shares"] if filled else 0.0, r["fee"] if filled else 0.0, r["book"]))
        print(f"{wallet}: {city} {day} сводка {t_obs:%H:%M}Z → максимум {mx}, вариант {rng[0]}..{rng[1]} мёртв; через {lag:.1f} мин "
              + (f"купили {r['shares']:.1f} «нет» по {r['avg']:.3f}" if filled else "не купили"), flush=True)
    conn.commit()


def check_watch(conn, books, watch, wallet=WALLET):
    """03.10: мёртвые варианты, где сразу после сводки дешёвого «нет» не было: раз в цикл смотрим стакан в памяти;
    появилось «нет» не дороже 1 − MIN_BID — покупаем (заявка доходит за ORDER_LAT). Данные 02-03.10: после того как мы
    посмотрели стакан, ещё 10% дешёвого «нет» продали — новые заявки появлялись через 1-4 мин."""
    cap = 1 - MIN_BID
    for key in list(watch):
        w = watch[key]
        if time.time() > w["until"]:
            watch.pop(key)
            continue
        asks = books.get(w["tok"]) or []
        if not asks or asks[0][0] > cap or depth(asks, cap) < MIN_FILL:
            continue
        time.sleep(ORDER_LAT)
        r = fill_from_asks(w["m"], books.get(w["tok"]) or [], STAKE, cap)
        if r["cost"] < MIN_FILL or cash(conn, wallet) < STAKE:
            continue
        watch.pop(key)
        now = datetime.now(timezone.utc)
        lag = (now - w["t_obs"]).total_seconds() / 60
        _, city, day, lo = key
        book = json.dumps({"before": asks[:5], "after": (books.get(w["tok"]) or [])[:5], "watch": True})
        reason = f"куплено при слежке: через {lag:.1f} мин после замера дешёвое «нет» появилось снова"
        cur = conn.execute("""UPDATE paper_obs_trades SET placed_at = ?, price = ?, stake = ?, status = 'open', reason = ?, shares = ?,
                              fee = ?, book_json = ? WHERE wallet = ? AND city = ? AND local_date = ? AND bucket_lo = ? AND status = 'nofill'""",
                           (now.isoformat(), r["avg"], r["cost"], reason, r["shares"], r["fee"], book, wallet, city, day, lo))
        if cur.rowcount == 0:
            conn.execute(
                """INSERT OR IGNORE INTO paper_obs_trades
                   (wallet, city, local_date, unit, bucket_lo, bucket_hi, obs_max, obs_time_utc, placed_at, yes_bid,
                    price, stake, status, reason, shares, fee, book_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (wallet, city, day, w["cfg"]["unit"], w["rng"][0], w["rng"][1], w["mx"], w["t_obs"].isoformat(), now.isoformat(),
                 float(w["m"].get("bestBid") or 0), r["avg"], r["cost"], "open", reason, r["shares"], r["fee"], book))
        conn.commit()
        print(f"{wallet}: {city} {day} вариант {w['rng'][0]}..{w['rng'][1]} — слежка: через {lag:.1f} мин купили "
              f"{r['shares']:.1f} «нет» по {r['avg']:.3f}", flush=True)


FRESH_SEC = 900   # сводки моложе 15 мин при старте — не «старые»: их обработает цикл (вдруг вышли во время перезапуска)


def seed(conn, now):
    """Максимум дня по уже вышедшим сводкам (aviationweather за 30 ч) — чтобы старое не считалось новым.
    01.10: перезапуск в :00 совпадал со сводками за :00 (Джидда, Карачи…) — их считали старыми и пропускали.
    Теперь свежие (моложе FRESH_SEC) в максимум не идут и остаются «новыми» для цикла."""
    mx, known = {}, set()
    try:
        r = requests.get(METAR_API, params={"ids": ",".join(ICAO_CITY), "hours": 30, "format": "json"}, timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        # 02.10: в 18:05 пропал DNS — запуск падал и obs_rt простоял час. Те же сводки уже есть у нас (metar_seen_src) — берём оттуда
        print(f"{WALLET}: aviationweather недоступен ({type(e).__name__}) — максимум дня по своим записям сводок", flush=True)
        since = datetime.fromtimestamp(now.timestamp() - 30 * 3600, timezone.utc).isoformat()
        r = [{"icaoId": i, "obsTime": int(datetime.fromisoformat(o).timestamp()), "temp": t}
             for i, o, t in conn.execute("SELECT icao, obs_time_utc, MAX(temp_c) FROM metar_seen_src WHERE obs_time_utc >= ? "
                                         "AND temp_c IS NOT NULL GROUP BY icao, obs_time_utc", (since,))]
    for m in r:
        city = ICAO_CITY.get(m.get("icaoId"))
        if not city or m.get("temp") is None or not m.get("obsTime"):
            continue
        if now.timestamp() - m["obsTime"] < FRESH_SEC:
            continue
        known.add((m["icaoId"], m["obsTime"]))
        cfg = OBS_CITIES[city]
        t = datetime.fromtimestamp(m["obsTime"], timezone.utc)
        k = (city, t.astimezone(ZoneInfo(cfg["tz"])).date())
        mx[k] = max(mx.get(k, -999), value(m["temp"], cfg))
    return mx, known


def main():
    from jobmark import mark_alive, single_instance
    single_instance("obs_rt")
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    day_max, known = seed(conn, now)
    feed, mk, alive = Feed(), Markets(), {"t": 0}
    no_tokens = []
    for c, cfg in OBS_CITIES.items():   # маркеты дня — заранее
        for _rng, m in mk.get(c, now.astimezone(ZoneInfo(cfg["tz"])).date()):
            try:
                no_tokens.append(json.loads(m["clobTokenIds"])[1])
            except (ValueError, KeyError, IndexError):
                pass
    books = Books(no_tokens)   # 02.10: стаканы «нет» всех вариантов дня — в памяти
    books.start()
    books.ready.wait(20)
    print(f"{WALLET}: стаканы в памяти — {len(books.asks)} из {len(no_tokens)} вариантов", flush=True)
    end = time.time() + LISTEN_MIN * 60
    last_awc, n_new = 0.0, 0
    last_nat, swob_done = 0.0, set()
    seen_src = set()
    watch = {}   # 03.10: мёртвые варианты под слежкой (check_watch)
    first_pass = True
    print(f"{WALLET}: старт, городов {len(OBS_CITIES)}, максимумов дня {len(day_max)}", flush=True)
    while time.time() < end:
        now = datetime.now(timezone.utc)
        got = [(icao, t, parse_metar_temp(" " + raw + " "), "tgftp_rt") for icao, d, h, m, raw in feed.poll(now)
               if (t := obs_time(d, h, m, now)) is not None]
        got += [(icao, t, parse_metar_temp(" " + raw + " "), "imgw_rt") for icao, d, h, m, raw in imgw(feed.s, now)
                if (t := obs_time(d, h, m, now)) is not None]
        nat_every = 1 if any(feed.in_window(i, now) for i in ("CYYZ", "LTAC", "LTFM")) else NAT_SEC   # 02.10: в окне — раз в секунду
        if time.time() - last_nat >= nat_every:   # национальные ленты: Канада, Турция (01.10)
            last_nat = time.time()
            got += canada(feed.s, now, swob_done) + mgm(feed.s, now)
        if first_pass:   # свежие сводки, вышедшие во время перезапуска, — тоже в обработку
            first_pass = False
            try:
                for m in requests.get(METAR_API, params={"ids": ",".join(ICAO_CITY), "hours": 1, "format": "json"}, timeout=15).json():
                    if m.get("obsTime") and m.get("temp") is not None and now.timestamp() - m["obsTime"] < FRESH_SEC:
                        got.append((m["icaoId"], datetime.fromtimestamp(m["obsTime"], timezone.utc), m["temp"], "awc_rt"))
            except (requests.RequestException, ValueError):
                pass
        if time.time() - last_awc >= AWC_SEC:
            last_awc = time.time()
            try:
                for m in requests.get(METAR_API, params={"ids": ",".join(ICAO_CITY), "hours": 1, "format": "json"}, timeout=15).json():
                    if m.get("obsTime") and m.get("temp") is not None:
                        got.append((m["icaoId"], datetime.fromtimestamp(m["obsTime"], timezone.utc), m["temp"], "awc_rt"))
            except (requests.RequestException, ValueError):
                pass
        for icao, t, temp, src in got:
            if temp is None:
                continue
            if (icao, int(t.timestamp()), src) not in seen_src:   # момент появления — по КАЖДОМУ источнику (сравнение вживую)
                seen_src.add((icao, int(t.timestamp()), src))
                try:
                    conn.execute("INSERT OR IGNORE INTO metar_seen_src (icao, obs_time_utc, source, first_seen_utc, temp_c) VALUES (?, ?, ?, ?, ?)",
                                 (icao, t.isoformat(), src, now.isoformat(), temp))
                    conn.commit()
                except sqlite3.OperationalError:
                    conn.rollback()
            if (icao, int(t.timestamp())) in known:
                continue
            known.add((icao, int(t.timestamp())))
            n_new += 1
            city = ICAO_CITY[icao]
            cfg = OBS_CITIES[city]
            day = t.astimezone(ZoneInfo(cfg["tz"])).date()
            v = value(temp, cfg)
            if day != now.astimezone(ZoneInfo(cfg["tz"])).date() or v <= day_max.get((city, day), -999):
                day_max[(city, day)] = max(day_max.get((city, day), -999), v)
                continue
            prev = day_max.get((city, day), -999)
            day_max[(city, day)] = v
            try:
                buy_dead(conn, city, cfg, day, v, prev, t, mk.get(city, day), src, books=books, watch=watch)
            except Exception as e:  # noqa: BLE001 — один город не роняет весь час
                conn.rollback()
                print(f"{WALLET}: {city} ошибка — {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        try:
            check_watch(conn, books, watch)
        except Exception as e:  # noqa: BLE001 — слежка не роняет цикл
            conn.rollback()
            print(f"{WALLET}: слежка — ошибка {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        mark_alive("weather_obs_rt", alive)
        time.sleep(POLL_SEC)
    print(f"{WALLET}: за запуск новых сводок {n_new}, баланс ${cash(conn, WALLET):.2f}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
