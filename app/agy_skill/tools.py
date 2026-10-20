"""Tools for forecasting today's maximum temperature at one airport station. Use from Python in this folder:

    from tools import *
    obs, days = load("miami")                       # all past observations / daily results (only days BEFORE today)
    peak_hours("miami")                             # at what local hour the daily max is usually reached
    rise_after("miami", "11:00", gap=0)             # how much the max still rose after this local time in this season
    analogs("miami", "11:00", temp_now=84.9, max_so_far=85, today="2026-10-13")   # most similar past days
    forecast_errors("miami", today="2026-10-13")    # how wrong each weather model and LightGBM were at this station
    bucket_probs(final_values, ["86-87", "88-89", ...], floor=85)                  # sample of final maxima -> chances

All temperatures are in the market unit of the city (see cities.json). Daily values are whole degrees as the market settles.
"""
import json
from datetime import date

import numpy as np
import pandas as pd

CITIES = json.load(open("cities.json"))


def load(city):
    obs = pd.read_csv(f"data/{city}_obs.csv")
    obs["minutes"] = obs.local_time.str[:2].astype(int) * 60 + obs.local_time.str[3:5].astype(int)
    days = pd.read_csv(f"data/{city}_days.csv")
    return obs, days


def _season(df, today, window):
    if today is None:
        return df
    t = date.fromisoformat(today)
    doy = pd.to_datetime(df.local_date).dt.dayofyear
    d = (doy - t.timetuple().tm_yday).abs()
    return df[np.minimum(d, 365 - d) <= window]


def _state(city, at, today=None, window=45):
    """Per past day: last temperature at/before local time `at`, max so far (whole units), final max, temp trend over 2 h."""
    obs, days = load(city)
    tcol = [c for c in obs.columns if c.startswith("temp_")][0]
    m = int(at[:2]) * 60 + int(at[3:5])
    unit = CITIES[city]["unit"]
    whole = (lambda x: np.floor(np.round((x - 32) * 5 / 9, 1) * 9 / 5 + 32 + 0.5)) if unit == "F" else (lambda x: np.floor(x + 0.5))
    obs = obs.assign(whole=whole(obs[tcol]))
    before = obs[obs.minutes <= m]
    g = before.groupby("local_date")
    st = pd.DataFrame({"temp_now": g[tcol].last(), "max_so_far": g["whole"].max(), "n": g.size()})
    two = obs[(obs.minutes <= m - 110) & (obs.minutes >= m - 130)].groupby("local_date")[tcol].last()
    st["trend_2h"] = st.temp_now - two
    st = st.join(days.set_index("local_date"))
    st = st.rename(columns={[c for c in st.columns if c.startswith("station_max_")][0]: "final_max"})
    st = st[st.n >= 3].reset_index()
    return _season(st, today, window)


def peak_hours(city, today=None, window=45):
    """Share of past days (this season) whose max was first reached in each local hour."""
    obs, days = load(city)
    tcol = [c for c in obs.columns if c.startswith("temp_")][0]
    obs = _season(obs, today, window)
    idx = obs.groupby("local_date")[tcol].idxmax()
    hrs = obs.loc[idx, "local_time"].str[:2].astype(int)
    return (hrs.value_counts(normalize=True).sort_index() * 100).round(1)


def rise_after(city, at, gap=None, today=None, window=45):
    """How much the final max exceeded the max so far at local time `at` (whole units), share of days in %.
    gap — only days where (max_so_far − temp_now) was within ±1 of gap (0 = temperature is at its max so far)."""
    st = _state(city, at, today, window)
    if gap is not None:
        st = st[((st.max_so_far - st.temp_now) - gap).abs() <= 1]
    r = (st.final_max - st.max_so_far).clip(lower=0)
    return {"days": int(len(r)), "rise_pct": (r.value_counts(normalize=True).sort_index() * 100).round(1).to_dict()}


def analogs(city, at, temp_now, max_so_far, trend_2h=None, today=None, k=20, window=45):
    """The k past days most similar at local time `at`: temperature now, max so far, 2-hour trend (if given).
    Returns the table and the final maxima of those days shifted to today's level (final − max_so_far + today's max_so_far)."""
    st = _state(city, at, today, window).dropna(subset=["final_max"])
    dist = (st.temp_now - temp_now).abs() + (st.max_so_far - max_so_far).abs()
    if trend_2h is not None:
        dist = dist + (st.trend_2h.fillna(0) - trend_2h).abs()
    st = st.assign(dist=dist).nsmallest(k, "dist")
    st["final_shifted"] = (st.final_max - st.max_so_far).clip(lower=0) + max_so_far
    return st[["local_date", "temp_now", "max_so_far", "trend_2h", "final_max", "polymarket_winner", "final_shifted", "dist"]]


def forecast_errors(city, today=None, window=None, recent=14):
    """Past daily-max forecasts vs the station max (data/<city>_forecasts.csv), per source and lead:
    lead 'live' = morning model run (as in the letter), 'day1' = issued the day before (longer history);
    lightgbm_v3 'morning' = the same morning forecast as in the letter, 'walkforward' = retrained only on the past.
    bias = mean(forecast − actual) (add −bias to correct today's forecast), mae, the same over the last `recent` days,
    and for LightGBM: share of days inside its p10..p90 (80% if its spread is right; less = it is overconfident).
    window — only days within ±window days of the season (None = all)."""
    f = pd.read_csv(f"data/{city}_forecasts.csv")
    f = _season(f, today, window) if window else f
    f = f.assign(err=f.forecast - f.station_max)
    last = sorted(f.local_date.unique())[-recent:]
    out = []
    for (src, lead), g in f.groupby(["source", "lead"]):
        r = g[g.local_date.isin(last)]
        row = {"source": src, "lead": lead, "days": len(g), "bias": round(g.err.mean(), 2), "mae": round(g.err.abs().mean(), 2),
               "recent_days": len(r), "recent_bias": round(r.err.mean(), 2) if len(r) else None,
               "recent_mae": round(r.err.abs().mean(), 2) if len(r) else None}
        if g.p10.notna().any():
            row["in_p10_p90_pct"] = round(100 * ((g.station_max >= g.p10) & (g.station_max <= g.p90)).mean(), 1)
        out.append(row)
    return pd.DataFrame(out).sort_values(["lead", "mae"]).reset_index(drop=True)


def bucket_probs(values, buckets, floor=None, smooth=0.5):
    """Sample of final maxima → chances by market bucket labels ('86-87', '22', '<= 77', '>= 96'), Laplace smoothing.
    Buckets entirely below `floor` (max already observed) get 0."""
    vals = np.asarray(list(values), float)
    out = {}
    for b in buckets:
        s = b.replace("°F", "").replace("°C", "").replace(" or below", "").replace(" or higher", "").strip()
        if "below" in b or s.startswith("<="):
            lo, hi = -1e9, float(s.lstrip("<= "))
        elif "higher" in b or s.startswith(">="):
            lo, hi = float(s.lstrip(">= ")), 1e9
        elif "-" in s[1:]:
            i = s.index("-", 1)
            lo, hi = float(s[:i]), float(s[i + 1:])
        else:
            lo = hi = float(s)
        if floor is not None and hi < floor:
            out[b] = 0.0
            continue
        out[b] = float(((vals >= lo) & (vals <= hi)).sum()) + smooth
    t = sum(out.values())
    return {b: round(v / t, 4) for b, v in out.items()}
