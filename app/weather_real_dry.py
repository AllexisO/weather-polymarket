"""
Настоящие заявки вхолостую (26.10, Alex «делай»; docs/PRD.md §4 п.2): для каждой новой бумажной ставки общего банка
(кошельки pool — fav, ml5_cal, ml5, ml4e_cal, ml_day) собрать ТУ ЖЕ заявку, что ушла бы на Polymarket через официальный
py-clob-client: маркет и доля (gamma), шаг цены, neg_risk, минимум долей (стакан CLOB), покупка «сразу» (FAK) на сумму ставки
не дороже цены бумажной покупки, — и подписать её ОДНОРАЗОВЫМ пустым ключом. Заявка никуда не отправляется, ключа счёта нет.

Что ловим до живых денег: маркет не найден / не принимает заявки, цена не ложится на шаг, долей меньше минимума стакана,
клиент не собирает заявку. Плюс сколько прошло от бумажной покупки и куда ушла лучшая цена продажи (стакан сейчас).
Итог — таблица real_dry (страница /real, проверка логики). Отправка настоящей заявки — отдельный шаг, по решению Alex.

Запуск: крон каждые 2 ч в :25 (после weather_paper в :10). docker compose run --rm collector weather_real_dry.py
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_UP

import requests
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

from jobmark import item_guard, mark
from weather_edge import CITIES, GAMMA, month_day_year_slug, parse_bucket

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
CLOB = "https://clob.polymarket.com"
# состав общего банка — как REAL_CANDIDATES["pool"] в dashboard.py (решение Alex 24.10)
POOL = ("fav", "ml5_cal", "ml5", "ml4e_cal", "ml_day")
LOOKBACK_H = float(os.environ.get("LOOKBACK_H", "6"))


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS real_dry (paper_id INTEGER PRIMARY KEY, wallet TEXT, city TEXT, local_date TEXT,
                    side TEXT, bucket_lo REAL, bucket_hi REAL, paper_price REAL, paper_shares REAL, paper_at TEXT,
                    token_id TEXT, tick REAL, neg_risk INTEGER, min_size REAL, limit_price REAL, amount REAL,
                    maker_amount TEXT, taker_amount TEXT, best_ask_now REAL, ok INTEGER, problem TEXT, built_at TEXT, delay_s REAL)""")


def find_token(city, local_date, lo, hi, side):
    slug = f"highest-temperature-in-{CITIES[city]['poly_slug']}-on-{month_day_year_slug(date.fromisoformat(local_date))}"
    events = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
    for m in (events[0]["markets"] if events else []):
        if parse_bucket(m["question"]) == (lo, hi):
            return m, json.loads(m["clobTokenIds"])[1 if side == "no" else 0]
    return None, None


def build(client, r):
    """→ dict для real_dry. problem — первая причина, по которой настоящая заявка не ушла бы."""
    side = r["side"] or "yes"
    out = {"token_id": None, "tick": None, "neg_risk": None, "min_size": None, "limit_price": None, "amount": None,
           "maker_amount": None, "taker_amount": None, "best_ask_now": None, "problem": None}
    m, tok = find_token(r["city"], r["local_date"], r["bucket_lo"], r["bucket_hi"], side)
    if not tok:
        out["problem"] = "маркет не найден в gamma"
        return out
    out["token_id"] = tok
    if m.get("closed") or not m.get("acceptingOrders", True):
        out["problem"] = "маркет закрыт или не принимает заявки"
        return out
    tick = client.get_tick_size(tok)
    nr = client.get_neg_risk(tok)
    book = client.get_order_book(tok)
    out.update(tick=float(tick), neg_risk=int(bool(nr)), min_size=float(book.min_order_size or 0))
    asks = sorted(float(a.price) for a in (book.asks or []))
    out["best_ask_now"] = asks[0] if asks else None
    # потолок цены — цена бумажной покупки, вверх до шага (настоящая заявка FAK купит не дороже)
    lim = float((Decimal(str(r["price"])) / Decimal(tick)).to_integral_value(ROUND_UP) * Decimal(tick))
    amount = round(r["stake"], 2)
    out.update(limit_price=lim, amount=amount)
    if not float(tick) <= lim <= 1 - float(tick):
        out["problem"] = f"цена {lim} вне шага {tick}..{1 - float(tick)}"
        return out
    if r["shares"] < out["min_size"]:
        out["problem"] = f"долей {r['shares']:.2f} меньше минимума стакана {out['min_size']:g}"
        return out
    o = client.create_market_order(MarketOrderArgs(token_id=tok, amount=amount, side=BUY, price=lim, order_type=OrderType.FAK),
                                   PartialCreateOrderOptions(tick_size=tick, neg_risk=nr))
    d = o.dict()
    out.update(maker_amount=str(d["makerAmount"]), taker_amount=str(d["takerAmount"]))
    if not d.get("signature"):
        out["problem"] = "клиент не подписал заявку"
    return out


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_H)).isoformat()
    q = ",".join("?" * len(POOL))
    rows = [dict(r) for r in conn.execute(
        f"""SELECT * FROM paper_trades WHERE wallet IN ({q}) AND stake > 0 AND price IS NOT NULL
            AND COALESCE(placed_at, snapshot_ts) >= ? AND id NOT IN (SELECT paper_id FROM real_dry)""", (*POOL, since))]
    conn.rollback()
    client = ClobClient(CLOB, key=Account.create().key.hex(), chain_id=137)   # пустой одноразовый ключ: только подпись
    n_ok = n_bad = 0
    for r in rows:
        with item_guard(f"{r['wallet']} {r['city']}", conn):
            out = build(client, r)
            now = datetime.now(timezone.utc)
            paper_at = r["placed_at"] or r["snapshot_ts"]
            delay = (now - datetime.fromisoformat(paper_at)).total_seconds()
            late = out["problem"] == "маркет закрыт или не принимает заявки" and delay > 3600
            if late:   # проверка через часы после покупки — маркет мог закрыться уже потом; не ошибка заявки
                out["problem"] = "проверка позже покупки — маркет уже закрыт"
            conn.execute("""INSERT OR REPLACE INTO real_dry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (r["id"], r["wallet"], r["city"], r["local_date"], r["side"] or "yes", r["bucket_lo"], r["bucket_hi"],
                          r["price"], r["shares"], paper_at, out["token_id"], out["tick"], out["neg_risk"], out["min_size"],
                          out["limit_price"], out["amount"], out["maker_amount"], out["taker_amount"], out["best_ask_now"],
                          None if late else int(out["problem"] is None), out["problem"], now.isoformat(), round(delay, 1)))
            conn.commit()
            n_ok += out["problem"] is None
            n_bad += out["problem"] is not None and not late
            print(f"{r['wallet']} {r['city']} {r['local_date']} {r['side'] or 'yes'} по {r['price']:.2f}: "
                  + ("заявка собрана и подписана" if out["problem"] is None else out["problem"] if late else f"НЕ УШЛА БЫ — {out['problem']}")
                  + (f"; лучшая продажа сейчас {out['best_ask_now']:.2f}" if out["best_ask_now"] is not None else ""))
    print(f"заявок вхолостую: {len(rows)}, собраны {n_ok}, не ушли бы {n_bad}, проверены поздно {len(rows) - n_ok - n_bad}")
    mark(conn, "weather_real_dry")
    conn.close()


if __name__ == "__main__":
    main()
