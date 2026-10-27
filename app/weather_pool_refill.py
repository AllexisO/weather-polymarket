"""
Докупка общего банка по живому стакану (27.10, Alex «давай делай»; проверка — data/research/_book_refill_*_1027.py).

Каждые 30 мин для каждой ставки общего банка (кошельки pool — fav, ml5_cal, ml5, ml4e_cal, ml_day), сделанной не раньше
6 ч назад, смотрим настоящий стакан Polymarket (CLOB /book) и докупаем ту же долю не дороже цены первой покупки (вверх до 1¢),
пока докупленное не дойдёт до суммы первой ставки (ставка вдвое — потолок). Строго: доли, которые банк уже купил по этой доле
(все бумажные ставки pool + прошлые докупки), считаем вынутыми из стакана — с дешёвого края; берём только то, что сверх.
Комиссия 0.05·p·(1−p) за долю всегда (как после 31.10), меньше 5 долей — не покупаем (минимум Polymarket).
Лучшая цена продажи дешевле 10¢ — не докупаем совсем, как правило YES_MIN у кошельков: на прошлом докупки < 5¢ — 48 шт., 1 выигрыш, −$43 на $147
(вариант уже «умер», маркет не закрыт; первый прогон 27.10 — Торонто по 0.1¢).

Проверка на прошлом (05-26.10, 560 ставок, стаканы mm_ws раз в минуту, настоящие итоги): цель $10 — первая покупка +5.8%,
докупка за 6 ч +39.3% на $2407 (шанс плюса по перемешиванию город-дней 97%; половины 05-15.10 +47%, 16-27.10 +33%);
цель $2 — докупка +$197 на $346. Только докупки ≥ 10¢: +26.4% на $2127 (шанс плюса 95%), но по половинам
05-15.10 +1.3%, 16-27.10 +47% — устойчиво только во второй половине. Итог докупки — итог бумажной ставки (тот же вариант), счёт — _real_pool(refill=True) в dashboard.

Запуск: крон каждые 30 мин (:12, :42). docker compose run --rm collector weather_pool_refill.py
"""
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import requests

from jobmark import item_guard, mark
from weather_real_dry import CLOB, POOL, find_token

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
SINCE = "2026-10-28"         # ставки на дни с 28.10 — двойник начинается с чистого листа
WINDOW_H = 6
FEE_RATE = 0.05
MIN_SHARES = 5
MIN_PRICE = 0.10             # = YES_MIN в weather_paper.py


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pool_refill (id INTEGER PRIMARY KEY, paper_id INTEGER, placed_at TEXT, wallet TEXT,
                    city TEXT, local_date TEXT, side TEXT, bucket_lo REAL, bucket_hi REAL, limit_price REAL, price REAL,
                    shares REAL, stake REAL, fee REAL, book_json TEXT)""")


def take(levels, skip_sh, money):
    """купить на money $ (с комиссией) с уровней [(цена, доли)] по возрастанию цены, пропустив skip_sh долей с дешёвого края."""
    sh = cost = fee = 0.0
    for p, s in levels:
        used = min(s, skip_sh)
        s, skip_sh = s - used, skip_sh - used
        per = p + FEE_RATE * p * (1 - p)
        k = min(s, (money - cost - fee) / per)
        if k > 1e-9:
            sh, cost, fee = sh + k, cost + k * p, fee + k * FEE_RATE * p * (1 - p)
    return sh, cost, fee


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    since = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_H)).isoformat()
    q = ",".join("?" * len(POOL))
    rows = [dict(r) for r in conn.execute(
        f"""SELECT * FROM paper_trades WHERE wallet IN ({q}) AND stake > 0 AND price IS NOT NULL AND status = 'open'
            AND local_date >= ? AND COALESCE(placed_at, snapshot_ts) >= ? ORDER BY id""", (*POOL, SINCE, since))]
    conn.rollback()
    n_buy = 0
    for r in rows:
        with item_guard(f"{r['wallet']} {r['city']}", conn):
            side = r["side"] or "yes"
            key = (r["city"], r["local_date"], r["bucket_lo"], r["bucket_hi"], side)
            done = conn.execute("SELECT COALESCE(SUM(stake + fee), 0) FROM pool_refill WHERE paper_id = ?", (r["id"],)).fetchone()[0]
            want = r["stake"] + (r["fee"] or 0) - done
            if want < 0.05:
                continue
            m, tok = find_token(r["city"], r["local_date"], r["bucket_lo"], r["bucket_hi"], side)
            if not tok or m.get("closed") or not m.get("acceptingOrders", True):
                continue
            book = requests.get(f"{CLOB}/book", params={"token_id": tok}, timeout=20).json()
            lim = math.ceil(r["price"] * 100 - 1e-6) / 100
            asks = sorted((float(a["price"]), float(a["size"])) for a in book.get("asks", []))
            if not asks or asks[0][0] < MIN_PRICE - 1e-9:   # продают дешевле 10¢ — вариант уже почти проиграл, не докупаем совсем
                continue
            levels = [(p, sz) for p, sz in asks if p <= lim + 1e-9]
            held = conn.execute(f"""SELECT COALESCE(SUM(shares), 0) FROM paper_trades WHERE wallet IN ({q}) AND stake > 0 AND city = ?
                                    AND local_date = ? AND bucket_lo IS ? AND bucket_hi IS ? AND COALESCE(side, 'yes') = ?""",
                                (*POOL, *key)).fetchone()[0]
            held += conn.execute("""SELECT COALESCE(SUM(shares), 0) FROM pool_refill WHERE city = ? AND local_date = ? AND bucket_lo IS ?
                                    AND bucket_hi IS ? AND side = ?""", key).fetchone()[0]
            sh, cost, fee = take(levels, held, want)
            if sh < MIN_SHARES:
                continue
            conn.execute("""INSERT INTO pool_refill (paper_id, placed_at, wallet, city, local_date, side, bucket_lo, bucket_hi, limit_price,
                            price, shares, stake, fee, book_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (r["id"], datetime.now(timezone.utc).isoformat(), r["wallet"], *key[:2], side, key[2], key[3], lim,
                          cost / sh, sh, cost, fee, str(levels[:5])))
            conn.commit()
            n_buy += 1
            print(f"{r['wallet']} {r['city']} {r['local_date']} {side} {r['bucket_lo']}-{r['bucket_hi']}: докупка {sh:.1f} долей "
                  f"по {cost / sh:.3f} (не дороже {lim:.2f}) на ${cost + fee:.2f}")
    print(f"ставок банка в окне {WINDOW_H} ч: {len(rows)}, докупок {n_buy}")
    mark(conn, "weather_pool_refill")
    conn.close()


if __name__ == "__main__":
    main()
