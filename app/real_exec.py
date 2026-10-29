"""
Настоящий счёт — ВХОЛОСТУЮ (29.10, Alex «давай»: подготовка к настоящим деньгам, PRD §4). Один счёт $100 на весь общий банк
(fav, ml5_cal, ml5, ml4e_cal, ml_day) — как будет вживую. Четыре части, всё без отправки заявок и без ключа счёта:

1. Заявка В МОМЕНТ РЕШЕНИЯ (а не через час, как weather_real_dry: там в среднем 64 мин после бумажной ставки). Утренние кошельки —
   из weather_fast_dry (сразу после решения), ml_day — его новые ставки в рабочей базе (опрос каждые 20 с). Заявка собирается и
   подписывается тем же кодом, что weather_real_dry.build (FAK на сумму ставки, не дороже цены бумажной покупки, пустой ключ).
2. ГАШЕНИЕ выигрышей: на Polymarket выигравшие доли надо погасить (redeem), иначе деньги стоят. В учёте деньги возвращаются на счёт
   только после гашения. Вхолостую гашение — пометка сразу после расчёта (redeem_dry). Настоящее — после выбора кошелька (заглушка).
3. СВЕРКА (reconcile): учёт счёта против бумажных ставок — у каждой заявки есть ставка, статусы и выплаты совпадают, деньги не в минусе,
   нет нерассчитанных ставок старше 2 суток после дня и непогашенных выигрышей старше 6 ч. Вживую — ещё баланс и позиции Polymarket.
4. ПРЕДЕЛЫ РИСКА (guard), числа — LIMITS: ставка ≤ $5; новые ставки за сутки ≤ $100; убыток за сутки ≥ $40 → пауза до конца суток;
   счёт (свободные + открытые по цене покупки) < $60 → остановка. По октябрю (5 кошельков банка) ни один предел не сработал бы:
   максимум ставки $3.95, за сутки $95.54, худший день −$39.75.

Таблица real_ledger — в data/db/fast_dry.sqlite3 (рядом с холостыми ставками). Рабочая база — только чтение.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = 100.0
LIMITS = {"bet_max": 5.0, "day_stake_max": 100.0, "day_loss_pause": 40.0, "equity_stop": 60.0}
POOL = ("fav", "ml5_cal", "ml5", "ml4e_cal", "ml_day")
FAST = {"fav", "ml5_cal", "ml5", "ml4e_cal"}          # решают в weather_fast_dry (холостые ставки — в той же базе)
REDEEM_LATE_H, UNSETTLED_LATE_H = 6, 48
REAL_MODE = False                                      # отправка настоящих заявок — только по решению Alex (PRD §4 п.5)


def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS main.real_ledger (id INTEGER PRIMARY KEY, src TEXT, paper_id INTEGER, wallet TEXT,
                 city TEXT, local_date TEXT, side TEXT, bucket_lo REAL, bucket_hi REAL, price REAL, shares REAL, cost REAL, fee REAL,
                 decided_at TEXT, built_at TEXT, latency_s REAL, token_id TEXT, limit_price REAL, signed INTEGER, problem TEXT,
                 blocked TEXT, status TEXT, payout REAL, settled_at TEXT, redeemed_at TEXT, UNIQUE (src, paper_id))""")
    c.commit()


def _now():
    return datetime.now(timezone.utc)


def state(c):
    """Счёт по учёту: свободные деньги, открытые (по цене покупки), за сегодня (UTC) — новые ставки и итог закрытых."""
    rows = c.execute("SELECT * FROM main.real_ledger WHERE status IN ('open', 'won', 'lost')").fetchall()
    spent = sum(r["cost"] + (r["fee"] or 0) for r in rows)
    back = sum(r["payout"] or 0 for r in rows if r["redeemed_at"])
    open_cost = sum(r["cost"] + (r["fee"] or 0) for r in rows if r["status"] == "open" or (r["status"] == "won" and not r["redeemed_at"]))
    today = _now().date().isoformat()
    day_stake = sum(r["cost"] + (r["fee"] or 0) for r in rows if (r["decided_at"] or "")[:10] == today)
    day_pnl = sum((r["payout"] or 0) - r["cost"] - (r["fee"] or 0) for r in rows if r["status"] in ("won", "lost") and (r["settled_at"] or "")[:10] == today)
    free = START - spent + back
    return {"free": free, "open": open_cost, "equity": free + open_cost, "day_stake": day_stake, "day_pnl": day_pnl, "n": len(rows)}


def guard(c, cost):
    """None — можно ставить; иначе причина (предел риска или нет денег)."""
    s = state(c)
    if s["equity"] < LIMITS["equity_stop"]:
        return f"остановка: счёт ${s['equity']:.2f} < ${LIMITS['equity_stop']:.0f}"
    if s["day_pnl"] <= -LIMITS["day_loss_pause"]:
        return f"пауза до конца суток: убыток за сутки ${s['day_pnl']:.2f}"
    if cost > LIMITS["bet_max"]:
        return f"ставка ${cost:.2f} > ${LIMITS['bet_max']:.0f}"
    if s["day_stake"] + cost > LIMITS["day_stake_max"]:
        return f"новые ставки за сутки ${s['day_stake'] + cost:.2f} > ${LIMITS['day_stake_max']:.0f}"
    if cost > s["free"] + 1e-9:
        return f"нет денег: свободно ${s['free']:.2f}"
    return None


_client = None


def submit(c, src, r):
    """Новая бумажная ставка банка (src 'dry' — fast_dry, 'live' — рабочая база) → предел риска → заявка собрана и подписана."""
    global _client
    if c.execute("SELECT 1 FROM main.real_ledger WHERE src = ? AND paper_id = ?", (src, r["id"])).fetchone():
        return None
    cost, fee = r["stake"], r["fee"] or 0.0
    decided = r["placed_at"] or r["snapshot_ts"]
    out = {"token_id": None, "limit_price": None, "problem": None}
    blocked = guard(c, cost + fee)
    if blocked is None:
        import weather_real_dry as rd
        if _client is None:
            from eth_account import Account
            from py_clob_client.client import ClobClient
            _client = ClobClient(rd.CLOB, key=Account.create().key.hex(), chain_id=137)   # пустой ключ — только подпись
        try:
            out = rd.build(_client, {**dict(r), "side": r["side"] or "yes"})
        except Exception as e:
            out["problem"] = f"{type(e).__name__}: {e}"[:200]
    now = _now()
    lat = (now - datetime.fromisoformat(decided)).total_seconds() if decided else None
    status = "blocked" if blocked else ("bad" if out["problem"] else "open")
    c.execute("""INSERT INTO main.real_ledger (src, paper_id, wallet, city, local_date, side, bucket_lo, bucket_hi, price, shares, cost, fee,
                 decided_at, built_at, latency_s, token_id, limit_price, signed, problem, blocked, status)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (src, r["id"], r["wallet"], r["city"], r["local_date"], r["side"] or "yes", r["bucket_lo"], r["bucket_hi"], r["price"],
               r["shares"], cost, fee, decided, now.isoformat(), lat, out.get("token_id"), out.get("limit_price"),
               int(not blocked and not out["problem"]), out["problem"], blocked, status))
    c.commit()
    return status


def collect(c):
    """Новые ставки банка: утренние — из холостых (main.paper_trades), ml_day — из рабочей базы (live.paper_trades), за 2 ч."""
    since = (_now() - timedelta(hours=2)).isoformat()
    n = 0
    for src, tbl, wallets in (("dry", "main.paper_trades", FAST), ("live", "live.paper_trades", {"ml_day"})):
        q = ",".join("?" * len(wallets))
        for r in c.execute(f"""SELECT * FROM {tbl} WHERE wallet IN ({q}) AND status IN ('open', 'won', 'lost') AND stake > 0
                               AND COALESCE(placed_at, snapshot_ts) >= ?""", (*sorted(wallets), since)).fetchall():
            n += submit(c, src, r) is not None
    return n


def settle(c):
    """Итог заявок — по их бумажным ставкам (тот же вариант и цена); выигрыш ждёт гашения."""
    for led in c.execute("SELECT * FROM main.real_ledger WHERE status = 'open'").fetchall():
        tbl = "main.paper_trades" if led["src"] == "dry" else "live.paper_trades"
        p = c.execute(f"SELECT status, payout, settled_at FROM {tbl} WHERE id = ?", (led["paper_id"],)).fetchone()
        if p and p["status"] in ("won", "lost"):
            c.execute("UPDATE main.real_ledger SET status = ?, payout = ?, settled_at = ? WHERE id = ?",
                      (p["status"], p["payout"] or 0.0, p["settled_at"] or _now().isoformat(), led["id"]))
    c.commit()


def redeem_dry(c):
    """Вхолостую: погасить выигрыши сразу. Вживую — гашение долей через Polymarket (настроить после выбора кошелька)."""
    if REAL_MODE:
        raise NotImplementedError("настоящее гашение — после выбора кошелька Polymarket (решение Alex)")
    c.execute("UPDATE main.real_ledger SET redeemed_at = ? WHERE status = 'won' AND redeemed_at IS NULL", (_now().isoformat(),))
    c.execute("UPDATE main.real_ledger SET redeemed_at = settled_at WHERE status = 'lost' AND redeemed_at IS NULL")
    c.commit()


def reconcile(c):
    """Список расхождений (пусто — всё сходится)."""
    bad = []
    now = _now()
    for led in c.execute("SELECT * FROM main.real_ledger WHERE status IN ('open', 'won', 'lost')").fetchall():
        tbl = "main.paper_trades" if led["src"] == "dry" else "live.paper_trades"
        p = c.execute(f"SELECT status, payout, stake, price FROM {tbl} WHERE id = ?", (led["paper_id"],)).fetchone()
        tag = f"{led['wallet']} {led['city']} {led['local_date']}"
        if p is None:
            bad.append(f"{tag}: у заявки нет бумажной ставки")
            continue
        if abs((p["stake"] or 0) - led["cost"]) > 0.005 or abs((p["price"] or 0) - (led["price"] or 0)) > 1e-6:
            bad.append(f"{tag}: сумма или цена заявки ≠ бумажной ставки")
        if p["status"] in ("won", "lost") and (p["status"] != led["status"] or abs((p["payout"] or 0) - (led["payout"] or 0)) > 0.005):
            bad.append(f"{tag}: итог учёта ≠ бумажной ставки")
        if led["status"] == "open":
            end = datetime.fromisoformat(led["local_date"]).replace(tzinfo=timezone.utc) + timedelta(days=1)
            if now - end > timedelta(hours=UNSETTLED_LATE_H):
                bad.append(f"{tag}: не рассчитана через {UNSETTLED_LATE_H} ч после дня")
        if led["status"] == "won" and not led["redeemed_at"] and now - datetime.fromisoformat(led["settled_at"]) > timedelta(hours=REDEEM_LATE_H):
            bad.append(f"{tag}: выигрыш не погашен {REDEEM_LATE_H}+ ч")
    if state(c)["free"] < -1e-6:
        bad.append("деньги счёта в минусе")
    return bad


def tick(c):
    """Один проход: новые заявки, расчёт, гашение. Возвращает число новых заявок."""
    ensure_schema(c)
    n = collect(c)
    settle(c)
    redeem_dry(c)
    return n


def compare(c):
    """Холостые быстрые решения против рабочих кошельков на тех же город-днях с сигналом (по кошельку)."""
    out = []
    filled = lambda st: st in ("open", "won", "lost")
    lag = lambda a, b: (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60 if a and b else None
    for w in sorted(FAST):
        rows = c.execute("""SELECT d.status ds, d.stake dst, d.payout dpo, d.fee dfee, d.snapshot_ts dts, d.placed_at dpa,
                                   l.status ls, l.stake lst, l.payout lpo, l.fee lfee, l.snapshot_ts lts, l.placed_at lpa
                            FROM main.paper_trades d JOIN live.paper_trades l ON l.wallet = d.wallet AND l.city = d.city AND l.local_date = d.local_date
                            WHERE d.wallet = ? AND d.status != 'skip'""", (w,)).fetchall()
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        out.append({"wallet": w, "n": len(rows),
                    "dry_filled": sum(filled(r["ds"]) for r in rows), "live_filled": sum(filled(r["ls"]) for r in rows),
                    "dry_lag": med([x for x in (lag(r["dts"], r["dpa"]) for r in rows if filled(r["ds"])) if x is not None]),
                    "live_lag": med([x for x in (lag(r["lts"], r["lpa"]) for r in rows if filled(r["ls"])) if x is not None]),
                    "dry_pnl": sum((r["dpo"] or 0) - (r["dst"] or 0) - (r["dfee"] or 0) for r in rows if r["ds"] in ("won", "lost")),
                    "live_pnl": sum((r["lpo"] or 0) - (r["lst"] or 0) - (r["lfee"] or 0) for r in rows if r["ls"] in ("won", "lost"))})
    return out


def summary(c):
    """Всё для страницы /real и для --report: счёт, заявки по статусам, блокировки, «не ушла бы», сверка, быстрые против обычных.
    Только чтение (страница открывает базу на чтение — таблицу создаёт weather_fast_dry)."""
    return {"state": state(c), "limits": LIMITS, "start": START,
            "statuses": [dict(r) for r in c.execute("SELECT status, COUNT(*) n, AVG(latency_s) lat FROM main.real_ledger GROUP BY status")],
            "blocked": [dict(r) for r in c.execute("SELECT blocked, COUNT(*) n FROM main.real_ledger WHERE blocked IS NOT NULL GROUP BY blocked")],
            "problems": [dict(r) for r in c.execute("SELECT problem, COUNT(*) n FROM main.real_ledger WHERE problem IS NOT NULL GROUP BY problem")],
            "last": [dict(r) for r in c.execute("""SELECT wallet, city, local_date, side, price, cost, status, latency_s, blocked, problem
                                                   FROM main.real_ledger ORDER BY id DESC LIMIT 10""")],
            "mismatch": reconcile(c), "compare": compare(c)}


def open_ro(dry_db, live_db):
    """Только чтение: база холостых решений + рабочая (для страницы /real). None — холостой базы ещё нет."""
    if not Path(dry_db).exists():
        return None
    c = sqlite3.connect(f"file:{dry_db}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute(f"ATTACH DATABASE 'file:{live_db}?mode=ro' AS live")
    if not c.execute("SELECT 1 FROM main.sqlite_master WHERE name = 'real_ledger'").fetchone():
        c.close()
        return None
    return c


def report(c):
    s = state(c)
    rows = c.execute("SELECT status, COUNT(*) n, AVG(latency_s) lat FROM main.real_ledger GROUP BY status").fetchall()
    print(f"счёт вхолостую: свободно ${s['free']:.2f}, в открытых ${s['open']:.2f}, всего ${s['equity']:.2f}; "
          f"сегодня новые ${s['day_stake']:.2f}, итог ${s['day_pnl']:+.2f}")
    for r in rows:
        print(f"  {r['status']:8}: {r['n']} заявок" + (f", от решения до подписанной заявки в среднем {r['lat']:.0f} с" if r["lat"] is not None else ""))
    for r in c.execute("SELECT blocked, COUNT(*) n FROM main.real_ledger WHERE blocked IS NOT NULL GROUP BY substr(blocked, 1, 20)").fetchall():
        print(f"  предел: {r['blocked']} — {r['n']}")
    for r in c.execute("SELECT problem, COUNT(*) n FROM main.real_ledger WHERE problem IS NOT NULL GROUP BY problem").fetchall():
        print(f"  не ушла бы: {r['problem']} — {r['n']}")
    bad = reconcile(c)
    print("сверка: " + ("всё сходится" if not bad else f"расхождений {len(bad)}: " + "; ".join(bad[:8])))
