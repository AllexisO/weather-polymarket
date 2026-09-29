"""
Гонка по 10-минутным данным KNMI для Амстердама (2026-09-29, зацепка из разбора HondaCivic: он торгует европейские
города днём). Вопрос: успевает ли открытый 10-минутный поток KNMI (Схипхол 06240, файл выходит через ~4 мин после
конца интервала) раньше, чем рынок Polymarket «сдаёт» вариант, который уже стал невозможным?

Событие: 10-минутный максимум KNMI впервые за день дошёл до T − 0.5 (METAR округлит до ≥ T) → варианты ниже T
«мертвы», если METAR это подтвердит. Смотрим: подтвердил ли METAR (итог дня ≥ T), и последняя цена «да» у
варианта сразу ниже T на момент выхода файла KNMI (конец интервала + DELAY). Цена ≥ 5¢ — было что забрать.
Данные: KNMI Open Data API (анонимный ключ, общий лимит), сделки poly_trades и METAR station_obs — из копии базы.
Кэш KNMI: /data/research/knmi_eham.json. Запуск (нужен netCDF4): python weather_knmi_race.py 2026-09-10 2026-09-24
"""
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KEY = "eyJvcmciOiI1ZTU1NGUxOTI3NGE5NjAwMDEyYTNlYjEiLCJpZCI6IjUzYTg1ZDBhMmQ5YzRkYzJiYWNlNzQ4NTQ2Zjk4ODExIiwiaCI6Im11cm11cjEyOCJ9"
API = "https://api.dataplatform.knmi.nl/open-data/v1/datasets/10-minute-in-situ-meteorological-observations/versions/1.0/files"
CACHE = Path("/data/research/knmi_eham.json")
TZ = ZoneInfo("Europe/Amsterdam")
DELAY_MIN = 4.5
DB = os.environ.get("POLY_LAB_DB", "/data/research/research.sqlite3")


def fetch(d0, d1):
    import netCDF4
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    s = requests.Session()
    s.headers["Authorization"] = KEY
    d = d0
    n = 0
    while d <= d1:
        t = datetime(d.year, d.month, d.day, 10, tzinfo=TZ).astimezone(timezone.utc)
        end = datetime(d.year, d.month, d.day, 18, tzinfo=TZ).astimezone(timezone.utc)
        while t <= end:
            k = t.strftime("%Y%m%d%H%M")
            if k not in cache:
                fn = f"KMDS__OPER_P___10M_OBS_L2_{k}.nc"
                for attempt in range(4):
                    r = s.get(f"{API}/{fn}/url", timeout=30)
                    if r.status_code == 429:
                        time.sleep(30)
                        continue
                    break
                if r.ok:
                    # ссылка на хранилище — без ключа KNMI (с заголовком Authorization хранилище отвечает ошибкой)
                    raw = requests.get(r.json()["temporaryDownloadUrl"], timeout=60).content
                    if not raw.startswith(b"\x89HDF") and not raw.startswith(b"CDF"):
                        print(f"  {k}: пришёл не файл — {raw[:120]!r}", flush=True)
                        cache[k] = None
                        n += 1
                        time.sleep(1.3)
                        t += timedelta(minutes=10)
                        continue
                    tmp = Path(f"/tmp/knmi_{k}.nc")  # HDF5 из памяти netCDF4 не открывает; свой файл и закрыть
                    tmp.write_bytes(raw)
                    ds = netCDF4.Dataset(str(tmp))
                    st = [str(x).strip() for x in ds.variables["station"][:]]
                    i = st.index("06240")
                    tx = float(ds.variables["tx"][:].flatten()[i])
                    ds.close()
                    tmp.unlink()
                    cache[k] = None if tx != tx else round(tx, 1)
                else:
                    cache[k] = None
                n += 1
                time.sleep(1.3)
                if n % 40 == 0:
                    CACHE.write_text(json.dumps(cache))
                    print(f"  загружено {n} файлов ({k})", flush=True)
            t += timedelta(minutes=10)
        d += timedelta(days=1)
    CACHE.write_text(json.dumps(cache))
    return cache


def analyse(cache, d0, d1):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    mk = {}
    for cid, lo, hi, dd in conn.execute("SELECT condition_id, bucket_lo, bucket_hi, local_date FROM poly_market_final WHERE city = 'amsterdam'"):
        mk.setdefault(dd, []).append((lo, hi, cid))
    out = []
    d = d0
    while d <= d1:
        ds = d.isoformat()
        fin = conn.execute("SELECT actual_max FROM weather_station_daily WHERE city = 'amsterdam' AND local_date = ?", (ds,)).fetchone()
        series = sorted((datetime.strptime(k, "%Y%m%d%H%M").replace(tzinfo=timezone.utc), v) for k, v in cache.items()
                        if v is not None and datetime.strptime(k, "%Y%m%d%H%M").replace(tzinfo=timezone.utc).astimezone(TZ).date() == d)
        if not series or not fin or ds not in mk:
            d += timedelta(days=1)
            continue
        run = -99.0
        for t_end, tx in series:
            if tx <= run:
                continue
            prev, run = run, tx
            for T in range(int(round(prev + 0.5)) if prev > -90 else int(round(tx)), int(round(tx + 1e-9)) + 1):
                if prev > -90 and prev >= T - 0.5:
                    continue
                if tx < T - 0.5:
                    continue
                dead = [(lo, hi, cid) for lo, hi, cid in mk[ds] if hi <= T - 0.5 + 1e-9 and hi > T - 1.5]  # вариант ровно ниже T
                if not dead:
                    continue
                lo, hi, cid = dead[0]
                avail = t_end + timedelta(minutes=DELAY_MIN)
                ts = avail.timestamp()
                row = conn.execute("""SELECT outcome, price FROM poly_trades WHERE condition_id = ? AND ts <= ? AND ts >= ?
                                      ORDER BY ts DESC LIMIT 1""", (cid, ts, ts - 3 * 3600)).fetchone()
                yes = None if row is None else (row[1] if row[0] == "Yes" else 1 - row[1])
                # когда рынок «сдал» вариант: первая сделка с ценой «да» ≤ 3¢ после того, как днём было ≥ 10¢
                coll = conn.execute("""SELECT MIN(ts) FROM poly_trades WHERE condition_id = ? AND ts >= ? AND
                                       ((outcome = 'Yes' AND price <= 0.03) OR (outcome = 'No' AND price >= 0.97))""",
                                    (cid, ts - 6 * 3600)).fetchone()[0]
                out.append({"date": ds, "T": T, "knmi_end": t_end.astimezone(TZ).strftime("%H:%M"), "tx": tx,
                            "metar_ok": fin[0] >= T, "yes_at_knmi": yes,
                            "lead_min": None if coll is None else round((coll - ts) / 60, 1)})
        d += timedelta(days=1)
    print(f"\nсобытий «KNMI впервые дошёл до порога варианта»: {len(out)}")
    ok = [e for e in out if e["metar_ok"]]
    print(f"METAR подтвердил (итог дня ≥ порога): {len(ok)} из {len(out)}")
    live = [e for e in ok if e["yes_at_knmi"] is not None and e["yes_at_knmi"] >= 0.05]
    print(f"из подтверждённых — «да» у мёртвого варианта ещё ≥ 5¢ в момент выхода файла KNMI: {len(live)} "
          f"(ещё ≥ 20¢: {sum(e['yes_at_knmi'] >= 0.2 for e in live)})")
    leads = sorted(e["lead_min"] for e in ok if e["lead_min"] is not None)
    if leads:
        q = lambda p: leads[int(p * (len(leads) - 1))]
        print(f"рынок сдал вариант (≤3¢) относительно выхода KNMI: медиана {q(.5):+.0f} мин (четверть {q(.25):+.0f}…{q(.75):+.0f}); "
              f"после KNMI: {sum(l > 0 for l in leads)} из {len(leads)}")
    gain = sum(e["yes_at_knmi"] for e in live)
    loss = sum(1 - (e["yes_at_knmi"] or 0) for e in out if not e["metar_ok"] and (e["yes_at_knmi"] or 0) >= 0.05)
    print(f"грубо, «нет» по $1 на каждый такой случай: выигрыш ~${gain:.2f} на подтверждённых, проигрыш ~${loss:.2f} на неподтверждённых")
    for e in out:
        print(f"  {e['date']} {e['knmi_end']} KNMI {e['tx']:.1f} → порог {e['T']}°: METAR {'да' if e['metar_ok'] else 'НЕТ'}, "
              f"«да» ниже порога {'—' if e['yes_at_knmi'] is None else f'{e['yes_at_knmi'] * 100:.1f}¢'}, "
              f"рынок сдал {'—' if e['lead_min'] is None else f'{e['lead_min']:+.0f} мин'}")


if __name__ == "__main__":
    d0, d1 = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    cache = fetch(d0, d1)
    analyse(cache, d0, d1)
