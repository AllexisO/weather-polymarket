"""
Data API Polymarket v2 — общий доступ для всех скриптов (03.11: v1 выключен — на любой запрос 410 «Data API v1 has been
retired»; с 02.11 ~22:30 UTC не собирались сделки, «Вхолостую» стоял — проверка биржи не получала ответа).

Отличия v2 (docs.polymarket.com/api-reference/data-api/overview): ответ — {"data": [...], "pagination": {...}}; поля
snake_case (token_id вместо asset, transaction_hash, event_slug); нет offset — страницы по курсору (pagination.next_cursor,
те же фильтры на каждой странице); start/end на /trades не фильтруют — останавливаемся по времени сами.
fetch() отдаёт строки и в прежнем виде (proxyWallet, asset, conditionId, eventSlug, outcomeIndex, transactionHash), чтобы
вызывающий код не менять. urllib, не requests: модуль зовёт и сайт (real_exec.exchange_halted), а в его контейнере requests нет.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://data-api.polymarket.com/v2"
V1_NAMES = {"proxy_wallet": "proxyWallet", "token_id": "asset", "condition_id": "conditionId", "event_slug": "eventSlug",
            "outcome_index": "outcomeIndex", "transaction_hash": "transactionHash", "usdc_size": "usdcSize"}


class DataApiError(ValueError):
    """Ошибка Data API (сеть, 4xx/5xx, ответ не того вида). ValueError — его уже ловят все вызывающие."""


def _get(path, params, timeout):
    url = f"{BASE}/{path}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-lab"})
            with urllib.request.urlopen(req, timeout=timeout) as f:
                d = json.load(f)
            if not isinstance(d, dict) or "data" not in d:
                raise DataApiError(f"data-api /{path}: ответ без data — {str(d)[:200]}")
            return d
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(float(e.headers.get("Retry-After") or 2 * (attempt + 1)))
                continue
            raise DataApiError(f"data-api /{path}: HTTP {e.code} — {e.read()[:200]!r}") from e
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise DataApiError(f"data-api /{path}: {type(e).__name__}: {e}") from e


def v1(row):
    """Строка v2 + прежние имена полей v1."""
    for new, old in V1_NAMES.items():
        if new in row:
            row.setdefault(old, row[new])
    return row


def fetch(path, params, limit=500, max_rows=None, since_ts=None, max_pages=400, need_all=False, timeout=30, pause=0.0):
    """Строки /v2/<path> (новые первыми, как в v1). Остановка: страниц больше нет, набрано max_rows, или самая старая строка
    страницы не новее since_ts. need_all — DataApiError, если упёрлись в max_pages, а страницы ещё есть (неполный список)."""
    out, cursor = [], None
    for _ in range(max_pages):
        d = _get(path, {**params, "limit": limit, "cursor": cursor}, timeout)
        batch = [v1(r) for r in (d.get("data") or []) if isinstance(r, dict)]
        out += batch
        pg = d.get("pagination") or {}
        cursor = pg.get("next_cursor")
        if not cursor or not pg.get("has_more", True):
            return out
        if max_rows and len(out) >= max_rows:
            return out[:max_rows]
        if since_ts is not None and batch and min(int(r["timestamp"]) for r in batch) <= since_ts:
            return out
        if pause:
            time.sleep(pause)
    if need_all:
        raise DataApiError(f"data-api /{path}: больше {max_pages} страниц по {limit} — список неполный")
    return out
