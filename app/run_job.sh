#!/bin/sh
# 2026-09-27 (после блокировки базы на 7 часов): у каждого скрипта крона есть предел времени.
# Зависший скрипт получает TERM, через 30 с — KILL; процесс умирает, SQLite откатывает его
# незавершённую запись, и база освобождается. Предел — JOB_TIMEOUT в секундах
# (по умолчанию 40 мин; 0 — без предела, для ручных долгих загрузок):
#   docker compose run --rm -e JOB_TIMEOUT=10800 collector weather_ml_live.py --train
LIMIT="${JOB_TIMEOUT:-2400}"
if [ "$LIMIT" = "0" ]; then exec python "$@"; fi
# не скрипт (python -c ..., -m ...) — без учёта в job_log, только предел времени
case "$1" in *.py) ;; *) exec timeout -s TERM -k 30 "$LIMIT" python "$@" ;; esac
# 2026-09-27: запуск через job_wrap.py — каждый запуск пишется в job_log (страница /status)
T0=$(date +%s)
timeout -s TERM -k 30 "$LIMIT" python /app/job_wrap.py "$@"
code=$?
if [ $code -eq 124 ] || [ $code -eq 137 ] || [ $code -eq 143 ]; then
  echo "$(date '+%F %T') ПРЕРВАНО по пределу времени ${LIMIT} с: $*" >&2
fi
# убит без возможности записаться (KILL) — записываем запуск отсюда
if [ $code -eq 137 ]; then python /app/jobmark.py "$1" 137 "$T0" || true; fi
exit $code
