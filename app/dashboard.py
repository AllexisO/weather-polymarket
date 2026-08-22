"""
Веб-дашборд поверх sqlite, который пишет weather_edge.py по крону.
Только чтение, ничего не торгует. Порт 8093, чтобы не пересекаться с
gold-sim (8090-8092).
"""

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="polymarket-lab dashboard")


def db():
    # Только чтение: cron пишет отдельным короткоживущим контейнером,
    # долгих блокировок не бывает, отдельный write-lock тут не нужен.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def unit_symbol(city_rows):
    return "°F" if city_rows and city_rows[0]["unit"] == "fahrenheit" else "°C"


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db()
    cities = [r["city"] for r in conn.execute("SELECT DISTINCT city FROM snapshots ORDER BY city")]

    cards = []
    for city in cities:
        latest_ts = conn.execute(
            "SELECT MAX(ts_utc) AS ts FROM snapshots WHERE city = ?", (city,)
        ).fetchone()["ts"]
        rows = conn.execute(
            """
            SELECT * FROM snapshots
            WHERE city = ? AND ts_utc = ?
            ORDER BY bucket_lo
            """,
            (city, latest_ts),
        ).fetchall()
        if not rows:
            continue
        best = max(rows, key=lambda r: abs(r["edge"]))
        cards.append(
            {
                "city": city,
                "local_date": rows[0]["local_date"],
                "local_hour": rows[0]["local_hour"],
                "unit": unit_symbol(rows),
                "best_edge": best["edge"],
                "best_lo": best["bucket_lo"],
                "best_hi": best["bucket_hi"],
                "market_p": best["market_p"],
                "model_p": best["model_p"],
                "event_vol": rows[0]["event_vol"],
                "n_snapshots": conn.execute(
                    "SELECT COUNT(DISTINCT ts_utc) AS n FROM snapshots WHERE city = ?", (city,)
                ).fetchone()["n"],
            }
        )
    conn.close()
    cards.sort(key=lambda c: abs(c["best_edge"]), reverse=True)
    return TEMPLATES.TemplateResponse("index.html", {"request": request, "cards": cards})


@app.get("/city/{city}", response_class=HTMLResponse)
def city_detail(request: Request, city: str):
    conn = db()
    latest_ts = conn.execute(
        "SELECT MAX(ts_utc) AS ts FROM snapshots WHERE city = ?", (city,)
    ).fetchone()["ts"]
    buckets = conn.execute(
        """
        SELECT * FROM snapshots WHERE city = ? AND ts_utc = ? ORDER BY bucket_lo
        """,
        (city, latest_ts),
    ).fetchall()

    history = conn.execute(
        """
        SELECT ts_utc, local_date, local_hour,
               MAX(ABS(edge)) AS max_abs_edge
        FROM snapshots
        WHERE city = ?
        GROUP BY ts_utc
        ORDER BY ts_utc DESC
        LIMIT 100
        """,
        (city,),
    ).fetchall()
    conn.close()

    return TEMPLATES.TemplateResponse(
        "city.html",
        {
            "request": request,
            "city": city,
            "unit": unit_symbol(buckets),
            "buckets": buckets,
            "history": history,
        },
    )


@app.get("/sports", response_class=HTMLResponse)
def sports(request: Request):
    conn = db()
    matches = []
    if table_exists(conn, "sports_snapshots"):
        latest_ts = conn.execute("SELECT MAX(ts_utc) AS ts FROM sports_snapshots").fetchone()["ts"]
        if latest_ts:
            rows = conn.execute(
                "SELECT * FROM sports_snapshots WHERE ts_utc = ? ORDER BY poly_slug", (latest_ts,)
            ).fetchall()
            by_slug = {}
            for r in rows:
                m = by_slug.setdefault(
                    r["poly_slug"],
                    {
                        "league": r["league"],
                        "home_team": r["home_team"],
                        "away_team": r["away_team"],
                        "commence_time": r["commence_time"],
                        "event_vol": r["event_vol"],
                        "outcomes": {},
                    },
                )
                m["outcomes"][r["outcome"]] = {"market_p": r["market_p"], "pinnacle_p": r["pinnacle_p"], "edge": r["edge"]}
            matches = list(by_slug.values())
            for m in matches:
                m["best_abs_edge"] = max(abs(o["edge"]) for o in m["outcomes"].values())
            matches.sort(key=lambda m: m["best_abs_edge"], reverse=True)
    conn.close()
    return TEMPLATES.TemplateResponse("sports.html", {"request": request, "matches": matches})
