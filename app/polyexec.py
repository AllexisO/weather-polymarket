"""
Симуляция исполнения на Polymarket — максимально близко к тому, как это
сделал бы реальный код (2026-09-24, просьба Alex: "чтобы при переходе на
реальные деньги не было ошибок и всё работало как нужно").

Что учитываем:
- КОМИССИЯ. На погодных маркетах она есть (feesEnabled, feeType
  "weather_fees", feeSchedule rate=0.05, exponent=1, takerOnly): платит
  тот, кто забирает готовую заявку — то есть мы. Формула из документации
  Polymarket: fee = доли × rate × (p × (1 − p))^exponent. Для $5 по 20¢ —
  ~$0.20 (4% ставки). Ставку $STAKE считаем ВМЕСТЕ с комиссией.
  Параметры берём из самого маркета (feeSchedule), не зашиваем.
- ПРАВИЛА ЗАЯВКИ: минимум orderMinSize долей (сейчас 5), шаг цены
  orderPriceMinTickSize (0.001).
- ЗАДЕРЖКА: стакан читается, через LATENCY_S секунд — ещё раз, и
  исполнение идёт по второму. Реальная заявка тратит время на подпись и
  отправку; за это время лучшие заявки могут забрать.
- АВАРИЙНАЯ ОСТАНОВКА: если существует файл data/STOP — новые ставки не
  делаются (для реальных денег — "красная кнопка").
- ВЫПЛАТА — по фактической цене закрытия нашей доли (outcomePrices после
  closed=true: 1, 0 или 0.5 при отмене), а не по сравнению бакетов.
- Лучшие заявки стакана в момент покупки пишем в сделку (book_json) —
  чтобы любую сделку можно было перепроверить.
"""

import json
import os
import time
from pathlib import Path

import requests

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
LATENCY_S = 2.0
STOP_FILE = Path(os.environ.get("POLY_LAB_DB", "/data/db/x")).parent.parent / "STOP"


def trading_stopped():
    return STOP_FILE.exists()


def fee_params(market):
    """(rate, exponent) из feeSchedule маркета; (0, 1), если комиссии нет."""
    if not market.get("feesEnabled"):
        return 0.0, 1.0
    fs = market.get("feeSchedule") or {}
    return float(fs.get("rate", 0.0)), float(fs.get("exponent", 1.0))


def fee_for(shares, price, rate, exponent):
    return shares * rate * (price * (1 - price)) ** exponent


def _book(token):
    return requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=20).json()


def _asks(token):
    """Предложения на покупку токена. /book уже отдаёт стакан с зеркалом
    парного токена (проверено 2026-09-24: Yes bid 13¢ ↔ No ask 87¢),
    поэтому второй стакан НЕ добавляем — иначе одни и те же заявки
    посчитались бы дважды."""
    return sorted((float(a["price"]), float(a["size"])) for a in _book(token).get("asks", []))


def simulate_buy(market, token, budget, max_price):
    """Покупка токена на сумму до budget (вместе с комиссией), только по
    заявкам не дороже max_price. Возвращает dict:
    shares, cost (без комиссии), fee, avg, min_ask, book (топ-5 до
    задержки), reason (если не исполнилось)."""
    rate, exponent = fee_params(market)
    min_size = float(market.get("orderMinSize") or 5)
    before = _asks(token)
    time.sleep(LATENCY_S)
    asks = _asks(token)
    out = {"shares": 0.0, "cost": 0.0, "fee": 0.0, "avg": None,
           "min_ask": asks[0][0] if asks else None,
           "book": json.dumps({"before": before[:5], "after": asks[:5]}), "reason": None}
    # Минимальная заявка — min_size долей: если на бюджет выходит меньше
    # (дорогой вариант), покупаем ровно минимум, как пришлось бы вживую.
    if asks and asks[0][0] <= max_price:
        p0 = asks[0][0]
        need = min_size * (p0 + rate * (p0 * (1 - p0)) ** exponent) * 1.001
        budget = max(budget, need)
    left = budget
    for price, size in asks:
        if price > max_price:
            break
        per_share = price + rate * (price * (1 - price)) ** exponent
        take = min(size, left / per_share)
        if take <= 0:
            break
        out["shares"] += take
        out["cost"] += take * price
        out["fee"] += fee_for(take, price, rate, exponent)
        left -= take * per_share
    if out["shares"] > 0:
        out["avg"] = out["cost"] / out["shares"]
    if not asks:
        out["reason"] = "продавцов в стакане нет"
    elif out["shares"] == 0:
        out["reason"] = f"продавали от {asks[0][0] * 100:.1f}¢, а выгодно — не дороже {max_price * 100:.1f}¢"
    elif out["shares"] < min_size:
        out["reason"] = (f"по выгодной цене продавали только {out['shares']:.1f} долей, "
                         f"а минимальная заявка на Polymarket — {min_size:.0f}")
        out.update(shares=0.0, cost=0.0, fee=0.0, avg=None)
    return out


def final_price(event_slug, bucket, side, parse_bucket, cache):
    """Цена закрытия нашей доли (Yes/No) для бакета, или None, если маркет
    ещё не закрыт. cache — dict на один прогон, чтобы не дёргать API
    повторно по тому же событию."""
    if event_slug not in cache:
        r = requests.get(f"{GAMMA}/events", params={"slug": event_slug}, timeout=20).json()
        cache[event_slug] = r[0]["markets"] if r else []
    for m in cache[event_slug]:
        if parse_bucket(m["question"]) != bucket:
            continue
        if not m.get("closed"):
            return None
        prices = [float(p) for p in json.loads(m["outcomePrices"])]
        outcomes = json.loads(m["outcomes"])
        return prices[outcomes.index("Yes" if side == "yes" else "No")]
    return None


# ---------------------------------------------------------------------------
# Своя (лимитная) заявка вместо покупки по чужой — 2026-09-24.
#
# Зачем: комиссию на погоде платит только тот, кто забирает готовую заявку
# (takerOnly), а выставивший заявку ещё и получает часть комиссии назад
# (rebate — не учитываем, консервативно). И покупаем по нижней цене
# стакана, а не по верхней: разница 1-3¢ — это 5-15% ставки при 20¢.
# Минус — заявка может не исполниться, и исполняется она чаще тогда,
# когда цена идёт против нас (продают нам, потому что знают больше).
# Симуляция это честно учитывает: исполнение — только по реальным
# сделкам ПОСЛЕ постановки заявки, с учётом очереди перед нами.

DATA_API = "https://data-api.polymarket.com"


def place_limit(market, token, budget, max_price):
    """Где встала бы наша заявка на покупку. Возвращает dict: limit (цена),
    shares (сколько хотим купить), queue_ahead (сколько долей стоит в
    очереди перед нами по нашей цене и выше), book, reason (если нельзя)."""
    tick = float(market.get("orderPriceMinTickSize") or 0.001)
    min_size = float(market.get("orderMinSize") or 5)
    book = _book(token)
    bids = sorted(((float(b["price"]), float(b["size"])) for b in book.get("bids", [])), reverse=True)
    asks = sorted((float(a["price"]), float(a["size"])) for a in book.get("asks", []))
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 1.0
    limit = min(max_price, round(best_bid + tick, 6))
    if limit >= best_ask:
        limit = round(best_ask - tick, 6)
    out = {"limit": limit, "shares": 0.0, "queue_ahead": 0.0,
           "book": json.dumps({"bids": bids[:5], "asks": asks[:5]}), "reason": None}
    if limit <= 0:
        out["reason"] = "некуда поставить заявку (стакан пуст или цена вне диапазона)"
        return out
    out["queue_ahead"] = sum(size for price, size in bids if price >= limit - 1e-9)
    out["shares"] = max(budget / limit, min_size)  # не меньше минимальной заявки
    if out["shares"] < min_size:
        out["reason"] = f"на ${budget:.0f} по {limit*100:.1f}¢ выходит меньше минимальных {min_size:.0f} долей"
    return out


def trades_since(condition_id, since_ts, until_ts, page=500, max_pages=20):
    """Все сделки маркета (оба исхода) в интервале, по времени по возрастанию."""
    out = []
    for i in range(max_pages):
        batch = requests.get(f"{DATA_API}/trades",
                             params={"market": condition_id, "limit": page, "offset": i * page}, timeout=30).json()
        if not batch:
            break
        out += [t for t in batch if since_ts < t["timestamp"] <= until_ts]
        if min(t["timestamp"] for t in batch) <= since_ts:
            break
    return sorted(out, key=lambda t: t["timestamp"])


def maker_filled(trades, limit, queue_ahead, want):
    """Сколько долей Yes купила бы наша заявка по цене limit.
    Сделка "продают Yes" — это taker SELL Yes по p или taker BUY No по 1-p
    (биржа сводит их с заявками на покупку Yes). Цена ниже нашей — значит,
    нашу заявку уже забрали; цена ровно наша — сначала съедается очередь
    перед нами."""
    filled, ahead = 0.0, queue_ahead
    for t in trades:
        side, outcome, p, size = t.get("side"), t.get("outcome"), float(t["price"]), float(t["size"])
        if outcome == "Yes" and side == "SELL":
            eff = p
        elif outcome == "No" and side == "BUY":
            eff = 1 - p
        else:
            continue
        if eff > limit + 1e-9:
            continue
        if abs(eff - limit) <= 1e-9 and ahead > 0:
            used = min(ahead, size)
            ahead -= used
            size -= used
        filled += size
        if filled >= want:
            return want
    return filled
