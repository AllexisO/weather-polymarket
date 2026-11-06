"""
Сводки METAR aviationweather.gov по многим станциям (06.11, полная проверка кошельков).

AWC отдаёт не больше 400 строк на запрос, самые новые: 47 станций за 30-36 ч обрезались до последних ~4.5 ч
(obs_tmin моделей v3-v5 в обычном снимке считался с ~03:30, а не с полуночи; «максимум на сейчас» obs / obs_speci —
только по последним часам; письма LLM после 10:00 — без ночных сводок). Поэтому запросы по CHUNK станций:
30-36 ч ≈ 60-75 строк на получасовую станцию со SPECI. Пачка 5 станций за 36 ч на 06.11 — до 364 строк: пачка, упёршаяся в
LIMIT, делится пополам и запрашивается заново (одна станция в LIMIT — ValueError: часы не помещаются).

Ошибки — как у requests: RequestException (сеть, HTTP) или ValueError (пустое тело, не JSON, не список);
повторные попытки — у вызывающего, как было.
"""

import requests

URL = "https://aviationweather.gov/api/data/metar"
CHUNK = 5
LIMIT = 400


def _get(ids, hours, timeout):
    r = requests.get(URL, params={"ids": ",".join(ids), "hours": hours, "format": "json"}, timeout=timeout)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise ValueError(f"ответ не список: {str(rows)[:100]}")
    if len(rows) >= LIMIT:
        if len(ids) == 1:
            raise ValueError(f"{ids[0]}: {len(rows)} строк за {hours} ч — ответ обрезан")
        h = len(ids) // 2
        return _get(ids[:h], hours, timeout) + _get(ids[h:], hours, timeout)
    return rows


def metars(ids, hours, timeout=30):
    ids = list(dict.fromkeys(i for i in ids if i))
    out = []
    for k in range(0, len(ids), CHUNK):
        out += _get(ids[k:k + CHUNK], hours, timeout)
    return out
