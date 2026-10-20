# Skill: today's maximum temperature at one airport station (Polymarket "Highest temperature")

You forecast the chance of every market bucket for TODAY's maximum temperature at one airport station.
You have Python 3 with pandas/numpy and this folder:

- `cities.json` — station, market unit (°F or °C), time zone, how the market settles;
- `data/<city>_obs.csv` — every past station observation since 2025-06-01, local date/time, temperature with tenths,
  dew point, wind, sky. Only days BEFORE today — today's observations are in the letter;
- `data/<city>_days.csv` — every past day: station max in whole market units and the Polymarket winning bucket (blank when
  there was no market). The station max matches the winner on ~99.5% of days; Paris before 2026-05 settled on another
  station — use `station_max` for statistics;
- `data/<city>_forecasts.csv` — past daily-max forecasts vs the station max: every weather model of the letter (lead `live` =
  the morning run as in the letter, since 2026-09-22; `day1` = issued the day before, since 2025-06) and our LightGBM v3
  (`morning` = the same forecast as in the letter, since 2026-09-27; `walkforward` = retrained only on the past, 2026-07..09);
- `tools.py` — ready functions (read its docstring first: `python3 -c "import tools; help(tools)"`).

## How the market settles (facts, checked against real outcomes)

1. The result is the highest METAR temperature of the local calendar day (00:00-23:59 local), whole degrees of the market unit.
2. °F cities: METAR carries tenths of °C; the value is converted to °F and rounded half up. 28.3 °C = 82.9 °F → 83.
3. The final max can never be below the max already observed today (given in the letter as "Max observed so far").
4. Observations come about every hour (US: at :51-:53); extra "SPECI" reports may add a higher value between them.
5. The market price of YES ≈ the crowd's probability. It is usually well calibrated — you add value only where the data
   clearly say something the price does not.

## Procedure — do it with code, not by feel

1. Read the letter: local time now, max so far, today's observations, remaining hourly forecast, model maxima, market prices.
2. `peak_hours(city, today=...)` — is the usual peak hour already behind or ahead?
3. `rise_after(city, "<HH:MM now>", gap=<max_so_far − temp_now>, today=...)` — how often and by how much the max still rose
   from this point in this season.
4. `analogs(city, "<HH:MM>", temp_now, max_so_far, trend_2h, today=...)` — the most similar past days; look at
   `final_shifted` (their rise applied to today's max so far).
5. `forecast_errors(city, today=...)` — which weather models and how much LightGBM to trust AT THIS STATION: correct each
   model maximum of the letter by its bias (prefer `live`, use `day1` when `live` has few days; `recent_*` shows drift),
   weight models by low mae. For LightGBM: its mae vs the best models and `in_p10_p90_pct` (below 80% = too confident,
   widen it). Also compare the remaining hourly forecast with today's observations so far and shift it by that error.
6. Combine into one distribution of today's final max, turn it into bucket chances (`bucket_probs` helps), and compare with
   the market. If your evidence is weak, stay close to the market — a confident wrong answer costs much more than a cautious one.

Spend at most a few minutes. Do not modify the data files. Do not access the network.

## Answer

Reply with ONE JSON object only, nothing else:
`{"max_f": <most likely final max, market unit>, "probs": {"<bucket label exactly as in the letter>": <0-1>, ...},
"why": "<English, at most 25 words: the key numbers you used>"}`
`probs` must contain every bucket of the letter and sum to 1. Buckets below the max already observed must be 0.
