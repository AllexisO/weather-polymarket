"""
Рейтинг сильных трейдеров на погоде — для кошелька «повтор» (weather_copy.py),
2026-09-26 (пункт 1 от Alex). Раз в сутки после сбора сделок.

Сильный = за последние WINDOW_DAYS дней ≥ MIN_TRADES сделок, итог в плюсе и
≥ MIN_PCT от оборота (сделка держится до итога маркета). В кошелёк идут TOP_N
лучших по итогу. На проверке (отбор по первым 2 неделям, повтор в следующие 3):
такие трейдеры, повторённые с задержкой ≤10 мин, — около нуля в целом, +1.7% на
покупках накануне; повторять всех подряд «в плюсе» — минус 1.6-2.7%.

Кошельки трейдеров — в poly_trades.wallet и poly_trade_wallets (с 26.09 сбор
ночью). Если за окно у сделок мало кошельков (< MIN_COVERAGE), рейтинг НЕ
пересчитывается — остаётся прошлый.
Пишет sharp_wallets. Запуск: python weather_sharp_rank.py [--source путь_к_базе]
(--source — читать сделки из другой базы, например из исследовательской копии).
"""

import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
WINDOW_DAYS = 14
MIN_TRADES = 100
MIN_PCT = 0.05
TOP_N = 30
MIN_COVERAGE = 0.8


def rank(src):
    until = src.execute("SELECT MAX(ts) FROM poly_trades").fetchone()[0]
    since = until - WINDOW_DAYS * 86400
    final = dict(src.execute("SELECT condition_id, final_yes FROM poly_market_final").fetchall())
    wallets = {}
    if src.execute("SELECT 1 FROM sqlite_master WHERE name = 'poly_trade_wallets'").fetchone():
        for k in src.execute("""SELECT tx, asset, ts, price, size, side, wallet FROM poly_trade_wallets
                                WHERE ts >= ?""", (since,)).fetchall():
            wallets[k[:6]] = k[6]
    has_col = "wallet" in [r[1] for r in src.execute("PRAGMA table_info(poly_trades)")]
    rows = src.execute("SELECT tx, asset, ts, price, size, side, condition_id, outcome"
                       + (", wallet" if has_col else ", NULL") + " FROM poly_trades WHERE ts >= ?", (since,)).fetchall()
    st = defaultdict(lambda: [0, 0.0, 0.0])
    n_all = n_w = 0
    for tx, asset, ts, price, size, side, cid, outc, w in rows:
        n_all += 1
        w = w or wallets.get((tx, asset, ts, price, size, side))
        if not w or cid not in final:
            continue
        n_w += 1
        fin = final[cid] if outc == "Yes" else 1 - final[cid]
        s = st[w]
        s[0] += 1
        s[1] += size * (fin - price) if side == "BUY" else size * (price - fin)
        s[2] += size * price
    coverage = n_w / max(n_all, 1)
    good = [(w, *s) for w, s in st.items() if s[0] >= MIN_TRADES and s[1] > 0 and s[1] / max(s[2], 1) >= MIN_PCT]
    good.sort(key=lambda x: -x[2])
    return good[:TOP_N], coverage, len(st)


def main():
    src_path = sys.argv[sys.argv.index("--source") + 1] if "--source" in sys.argv else DB_PATH
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=60)
    top, coverage, n = rank(src)
    src.close()
    print(f"трейдеров в окне {n}, доля сделок с известным кошельком {coverage:.0%}, сильных в рейтинге {len(top)}")
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS sharp_wallets (wallet TEXT PRIMARY KEY, n INTEGER, pnl REAL,
                    turnover REAL, ranked_at TEXT)""")
    if coverage < MIN_COVERAGE:
        print(f"мало данных о кошельках ({coverage:.0%} < {MIN_COVERAGE:.0%}) — оставляю прошлый рейтинг")
    else:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM sharp_wallets")
        conn.executemany("INSERT INTO sharp_wallets VALUES (?, ?, ?, ?, ?)", [(w, k, p, t, now) for w, k, p, t in top])
        conn.commit()
        for w, k, p, t in top[:10]:
            print(f"  {w[:10]}…  сделок {k:6d}  итог {p:+10,.0f}$  ({100 * p / t:+.1f}% от оборота)")
    from jobmark import mark
    mark(conn, "weather_sharp_rank")
    conn.close()


if __name__ == "__main__":
    main()
