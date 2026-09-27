#!/bin/sh
# 2026-09-27 (после блокировки базы на 7 часов): у каждого скрипта крона есть предел времени.
# Зависший скрипт получает TERM, через 30 с — KILL; процесс умирает, SQLite откатывает его
# незавершённую запись, и база освобождается. Предел — JOB_TIMEOUT в секундах
# (по умолчанию 40 мин; 0 — без предела, для ручных долгих загрузок):
#   docker compose run --rm -e JOB_TIMEOUT=10800 collector weather_ml_live.py --train
LIMIT="${JOB_TIMEOUT:-2400}"
if [ "$LIMIT" = "0" ]; then exec python "$@"; fi
timeout -s TERM -k 30 "$LIMIT" python "$@"
code=$?
if [ $code -eq 124 ] || [ $code -eq 137 ]; then
  echo "$(date '+%F %T') ПРЕРВАНО по пределу времени ${LIMIT} с: $*" >&2
fi
exit $code
