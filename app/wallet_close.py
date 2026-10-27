"""
Досрочное закрытие открытых ставок выключенного кошелька (27.10, Alex: «выключай и всё, что в открытых ставках, — закрывай»).

Как вживую: каждая открытая ставка продаётся сразу по лучшим ценам покупателей в стакане Polymarket (идём вниз по стакану,
пока не продадим все доли), комиссия 0.05·p·(1−p) за долю — как у покупки. Покупателей не хватило — непроданные доли
стоят 0 (так и есть вживую: продать нельзя, а итог уже не ждём). Выплата = выручка минус комиссия продажи;
status — won, если вышло больше, чем стоила покупка, иначе lost. В reason — метка EARLY_CLOSE: проверка кошельков
(weather_audit) такие ставки с итогом Polymarket не сверяет.

Запуск: docker compose run --rm --entrypoint python collector wallet_close.py <кошелёк> [--dry]
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

import requests

from weather_real_dry import CLOB, find_token

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
EARLY_CLOSE = "закрыта досрочно"
FEE_RATE = 0.05


def sell(r):
    """→ (выручка за вычетом комиссии, непродано долей, лучшая цена покупателя)."""
    side = r["side"] or "yes"
    _m, tok = find_token(r["city"], r["local_date"], r["bucket_lo"], r["bucket_hi"], side)
    if not tok:
        return 0.0, r["shares"], None
    book = requests.get(f"{CLOB}/book", params={"token_id": tok}, timeout=20).json()
    bids = sorted(((float(x["price"]), float(x["size"])) for x in book.get("bids", [])), reverse=True)
    left, got = r["shares"], 0.0
    for p, size in bids:
        k = min(left, size)
        got += k * p - k * FEE_RATE * p * (1 - p)
        left -= k
        if left <= 1e-9:
            break
    return got, max(left, 0.0), bids[0][0] if bids else None


def main():
    wallet, dry = sys.argv[1], "--dry" in sys.argv
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM paper_trades WHERE wallet = ? AND status = 'open' AND stake > 0", (wallet,))]
    conn.rollback()
    total = 0.0
    for r in rows:
        got, left, best = sell(r)
        cost = r["stake"] + (r["fee"] or 0)
        status = "won" if got > cost else "lost"
        note = (f" | {datetime.now(timezone.utc):%d.%m} {EARLY_CLOSE} (кошелёк выключен): продано {r['shares'] - left:.1f} из "
                f"{r['shares']:.1f} долей по стакану (лучшая цена покупателя {best if best is not None else 'нет'}), выручка ${got:.2f}")
        total += got - cost
        print(f"{r['city']} {r['local_date']} {r['side'] or 'yes'} {r['bucket_lo']}-{r['bucket_hi']}: куплено за ${cost:.2f}, "
              f"продано за ${got:.2f} → {got - cost:+.2f}")
        if not dry:
            conn.execute("UPDATE paper_trades SET status = ?, payout = ?, settled_at = ?, reason = COALESCE(reason, '') || ? "
                         "WHERE id = ? AND status = 'open'", (status, round(got, 6), datetime.now(timezone.utc).isoformat(), note, r["id"]))
            conn.commit()
    print(f"{wallet}: закрыто {len(rows)}{' (вхолостую)' if dry else ''}, итог {total:+.2f}")
    conn.close()


if __name__ == "__main__":
    main()
